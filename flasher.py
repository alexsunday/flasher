#!/usr/bin/env python
# encoding: utf-8

import os
import sys
import json
import time
import zlib
import serial
import hashlib
from io import BytesIO
from functools import partial

from serial.tools.list_ports import comports
from PyQt5.QtWidgets import QWidget, QApplication, QMessageBox, QMenu
from PyQt5.QtCore import QVariant, QUrl, QObject, QThread, pyqtSignal, QByteArray, QFile, QIODevice
from PyQt5 import QtNetwork

from dlg import Ui_Form
from esptool import ESP8266ROM, read_mac, erase_flash, ESPLoader, print_overwrite, _update_image_flash_params, pad_to, flash_size_bytes, detect_flash_size, DEFAULT_TIMEOUT


FLASH_BAUD = 576000
FIRMWARE_URL = "https://light.espush.cn/api/provider/firmware/?chip=ESP8266EX"


class Args(object):
    def __init__(self):
        self.compress = None
        self.no_compress = False
        self.no_stub = False
        self.encrypt = False
        self.flash_size = 'detect'
        self.flash_mode = ''
        self.flash_freq = ''
        self.addr_filename = []

    def set_body(self, addr, data):
        fobj = BytesIO(data)
        fobj.seek(0)
        self.addr_filename.append((addr, fobj))


class ESP8266Flasher(QObject):
    begin_flash_sig = pyqtSignal(dict)
    abort_flash_sig = pyqtSignal()
    sync_result_sig = pyqtSignal(int)
    flash_progress_sig = pyqtSignal(int, int)
    flash_result_sig = pyqtSignal(int, str)
    console_sig = pyqtSignal(str)
    begin_erase_flash_sig = pyqtSignal(str)
    complete_erase_flash_sig = pyqtSignal()

    def __init__(self, parent=None):
        QObject.__init__(self, parent)
        self._is_abort = False

    def show_log(self, s):
        self.console_sig.emit(s)

    def begin_flash(self, firmware):
        port = firmware['port']
        self._is_abort = False
        try:
            self.esp8266 = ESPLoader.detect_chip(port=port, baud=FLASH_BAUD)
            self.esp8266 = self.esp8266.run_stub()

        except serial.serialutil.SerialException as _:
            self.show_log(u'串口读写失败，请检查是否有其他程序占用了指定串口')
            self.flash_result_sig.emit(1, u'串口读写失败')
            return
        # self._flash_write(comport, firmwares)
        self.write_flash(firmware)
        self.flash_result_sig.emit(0, u'成功')
        self.esp8266.close_serial()

    def write_flash(self, firmware):
        args = Args()
        data = firmware['body']
        args.set_body(0, data)
        args.flash_mode = firmware['mode']
        args.flash_freq = firmware['speed']
        esp = self.esp8266

        if hasattr(args, "flash_size"):
            print("Configuring flash size...")
            detect_flash_size(esp, args)
            if args.flash_size != 'keep':
                esp.flash_set_parameters(flash_size_bytes(args.flash_size))

        print('Args: ', args)
        self._write_flash(esp, args)

    def _write_flash(self, esp, args):
        # set args.compress based on default behaviour:
        # -> if either --compress or --no-compress is set, honour that
        # -> otherwise, set --compress unless --no-stub is set
        if args.compress is None and not args.no_compress:
            args.compress = not args.no_stub

        print('before size change:', args)
        # verify file sizes fit in flash
        if args.flash_size != 'keep':  # TODO: check this even with 'keep'
            flash_end = flash_size_bytes(args.flash_size)
            for address, argfile in args.addr_filename:
                argfile.seek(0,2)  # seek to end
                if address + argfile.tell() > flash_end:
                    raise FatalError(("File %s (length %d) at offset %d will not fit in %d bytes of flash. " +
                                    "Use --flash-size argument, or change flashing address.")
                                    % (argfile.name, argfile.tell(), address, flash_end))
                argfile.seek(0)

        print('Arguments:[', args, ']')
        for address, argfile in args.addr_filename:
            if args.no_stub:
                print('Erasing flash...')
            image = pad_to(argfile.read(), 32 if args.encrypt else 4)
            if len(image) == 0:
                print('WARNING: File %s is empty' % argfile.name)
                continue
            image = _update_image_flash_params(esp, address, args, image)
            calcmd5 = hashlib.md5(image).hexdigest()
            uncsize = len(image)
            if args.compress:
                uncimage = image
                image = zlib.compress(uncimage, 9)
                ratio = uncsize / len(image)
                blocks = esp.flash_defl_begin(uncsize, len(image), address)
            else:
                ratio = 1.0
                blocks = esp.flash_begin(uncsize, address)
            argfile.seek(0)  # in case we need it again
            seq = 0
            written = 0
            t = time.time()
            while len(image) > 0:
                QApplication.processEvents()
                if self._is_abort:
                    self.flash_result_sig.emit(1, u'已终止')
                    self.esp8266.close_serial()
                    return

                print_overwrite('Writing at 0x%08x... (%d %%)' % (address + seq * esp.FLASH_WRITE_SIZE, 100 * (seq + 1) // blocks))
                sys.stdout.flush()
                block = image[0:esp.FLASH_WRITE_SIZE]
                if args.compress:
                    esp.flash_defl_block(block, seq, timeout=DEFAULT_TIMEOUT * ratio * 2)
                else:
                    # Pad the last block
                    block = block + b'\xff' * (esp.FLASH_WRITE_SIZE - len(block))
                    if args.encrypt:
                        esp.flash_encrypt_block(block, seq)
                    else:
                        esp.flash_block(block, seq)
                self.flash_progress_sig.emit(written + len(block), len(image))
                image = image[esp.FLASH_WRITE_SIZE:]
                seq += 1
                written += len(block)
            t = time.time() - t
            speed_msg = ""
            if args.compress:
                if t > 0.0:
                    speed_msg = " (effective %.1f kbit/s)" % (uncsize / t * 8 / 1000)
                print_overwrite('Wrote %d bytes (%d compressed) at 0x%08x in %.1f seconds%s...' % (uncsize, written, address, t, speed_msg), last_line=True)
            else:
                if t > 0.0:
                    speed_msg = " (%.1f kbit/s)" % (written / t * 8 / 1000)
                print_overwrite('Wrote %d bytes at 0x%08x in %.1f seconds%s...' % (written, address, t, speed_msg), last_line=True)

            if not args.encrypt:
                try:
                    res = esp.flash_md5sum(address, uncsize)
                    if res != calcmd5:
                        print('File  md5: %s' % calcmd5)
                        print('Flash md5: %s' % res)
                        print('MD5 of 0xFF is %s' % (hashlib.md5(b'\xFF' * uncsize).hexdigest()))
                        raise FatalError("MD5 of file does not match data in flash!")
                    else:
                        print('Hash of data verified.')
                except NotImplementedInROMError:
                    pass

        print('\nLeaving...')

        if esp.IS_STUB:
            # skip sending flash_finish to ROM loader here,
            # as it causes the loader to exit and run user code
            esp.flash_begin(0, 0)
            if args.compress:
                esp.flash_defl_finish(False)
            else:
                esp.flash_finish(False)

    def erase_flash(self, port):
        try:
            self.esp8266 = ESPLoader.detect_chip(port=port, baud=FLASH_BAUD)
            read_mac(self.esp8266, None)
            self.esp8266 = self.esp8266.run_stub()
        except serial.serialutil.SerialException as _:
            self.show_log(u'串口读写失败，请检查是否有其他程序占用了指定串口')
            self.flash_result_sig.emit(1, u'串口读写失败')
            return
        erase_flash(self.esp8266, None)
        self.show_log('flash 清除完毕')
        self.complete_erase_flash_sig.emit()
        self.esp8266.close_serial()

    def abort_flash(self):
        print('abort flash.')
        self._is_abort = True


class FlashMainWnd(QWidget):
    def __init__(self, parent=None):
        QWidget.__init__(self, parent)
        self.ui = Ui_Form()
        self.ui.setupUi(self)
        self.init_comports()
        self.init_romlist()
        self.init_btn()
        self.init_flasher_thread()

    def init_btn(self):
        self.change_btn_to_flash()

    def change_btn_to_flash(self):
        self.btn_state = 'FLASH'
        self.ui.gobtn.setText(u'开始')
        # 已写入字节数
        self.written = 0
        # 时间消耗
        self.elapse = time.time()

    def change_btn_to_abort(self):
        self.btn_state = 'ABORT'
        self.ui.gobtn.setText(u'终止')

    def action_state(self):
        return self.btn_state

    def closeEvent(self, evt):
        """
        :type evt: QCloseEvent
        :param evt:
        """
        self.flash_thread.exit()
        evt.accept()

    def init_comports(self):
        ports = comports()
        self.ui.com_box.clear()
        ports = [el.device for el in ports]
        for port in ports:
            self.ui.com_box.addItem(port)

    def init_romlist(self):
        req = QtNetwork.QNetworkRequest(QUrl(FIRMWARE_URL))
        self.rom_req_manager = QtNetwork.QNetworkAccessManager()
        self.rom_req_manager.finished.connect(self.rom_load_completed)
        self.rom_req_manager.get(req)

    def rom_load_completed(self, reply):
        err = reply.error()
        if err != QtNetwork.QNetworkReply.NoError:
            print('error......')
            return
        result = str(reply.readAll(), 'utf-8')
        rsp_obj = json.loads(result)
        for firm in rsp_obj:
            v = QVariant(firm)
            self.ui.firm_box.addItem(firm['title'], userData=v)

    def init_flasher_thread(self):
        self.flasher = ESP8266Flasher()
        self.flash_thread = QThread()
        self.flasher.moveToThread(self.flash_thread)
        self.flash_thread.start()
        # signal slot connect
        self.flasher.begin_erase_flash_sig.connect(self.flasher.erase_flash)
        self.flasher.complete_erase_flash_sig.connect(self.completed_erase)
        self.flasher.begin_flash_sig.connect(self.flasher.begin_flash)
        self.flasher.abort_flash_sig.connect(self.flasher.abort_flash)
        self.flasher.sync_result_sig.connect(self.sync_slot)
        self.flasher.flash_progress_sig.connect(self.flash_progress)
        self.flasher.flash_result_sig.connect(self.flash_result)
        self.flasher.console_sig.connect(self.show_log)

    def flash_result(self, res, desc):
        print('flash result is %r' % res)
        elapse = time.time() - self.elapse
        if res == 1:
            self.show_log(u'烧录失败 %s 耗时 %d 秒' % (desc, elapse))
        if res == 0:
            self.show_log(u'固件烧录成功, 耗时 %d 秒' % elapse)
            self.ui.progbar.setValue(100)
        self.change_btn_to_flash()

    def show_log(self, content):
        self.ui.textOut.append(content)

    def flash_progress(self, written, total):
        print('flash progress, total %d, writed %d' % (total, written))
        self.ui.progbar.setValue( (float(written) / total) * 100 )

    def sync_slot(self, res):
        print('connect result is %r' % res)
        if res == 0:
            self.show_log(u'串口同步成功，烧录即将进行')
        if res == 1:
            self.show_log(u'同步串口失败，请检查所选串口并重试')
            self.change_btn_to_flash()

    def start_btn_clicked(self):
        if self.action_state() == 'FLASH':
            self.change_btn_to_abort()
            self.go_flash()
        elif self.action_state() == 'ABORT':
            self.change_btn_to_flash()
            self.go_abort()

    def go_abort(self):
        self.show_log(u'准备终止烧录过程')
        self.flasher.abort_flash_sig.emit()

    def go_flash(self):
        device = self.ui.com_box.currentText()
        if device == "":
            self.show_log("选定一个串口，然后烧录固件")
            return
        firmware = self.ui.firm_box.itemData(self.ui.firm_box.currentIndex())
        self.download_firmware(firmware, partial(self.download_firmware_completed, firmware))
        firmware['port'] = device
        print(firmware)
        print(type(firmware))
        # firmfile = self.get_firmware(firmobj)
        # firmwares['app1'] = firmfile
        # self.show_log(u'固件准备完毕，准备烧录到 %s' % device)
        # self.flasher.begin_flash_sig.emit(device, firmwares)

    def download_firmware(self, firmware, cb):
        f_url = firmware["download"]
        req = QtNetwork.QNetworkRequest(QUrl(f_url))
        self.rom_down_manager = QtNetwork.QNetworkAccessManager()
        self.rom_down_manager.finished.connect(cb)
        self.rom_down_manager.get(req)

    def download_firmware_completed(self, firmware, reply):
        err = reply.error()
        if err != QtNetwork.QNetworkReply.NoError:
            print('error......')
            return
        print(firmware['checksum'])
        result = reply.readAll()
        print(type(result.data()))
        body = result.data()
        if firmware['checksum'].lower() != hashlib.md5(body).hexdigest().lower():
            self.show_log('checksum error!')
            return
        print(type(result))
        firmware['body'] = body
        self.show_log('固件下载完毕，准备烧录')
        self.flasher.begin_flash_sig.emit(firmware)

    def start_erase_clicked(self):
        device = self.ui.com_box.currentText()
        if device == "":
            self.show_log("选定一个串口，才能执行清除")
            return
        self.flasher.begin_erase_flash_sig.emit(device)
        self.ui.erasebtn.setEnabled(False)

    def completed_erase(self):
        self.ui.erasebtn.setEnabled(True)

    def refresh_port_list(self):
        self.init_comports()


def main():
    app = QApplication(sys.argv)
    widget = FlashMainWnd()
    widget.show()
    app.exec_()


if __name__ == '__main__':
    main()


'''
python -m PyQt5.uic.pyuic -x main.ui -o dlg.py
python -m PyQt5.pyrcc_main resource.qrc -o resource_rc.py
pyinstaller -F --noupx -w --win-no-prefer-redirects --clean --icon espush.ico helper.py
'''
