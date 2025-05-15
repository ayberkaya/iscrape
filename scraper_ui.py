import sys
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                            QHBoxLayout, QPushButton, QLabel, QComboBox, 
                            QProgressBar, QTextEdit, QMessageBox, QFileDialog, 
                            QDialog, QTabWidget, QCheckBox, QLineEdit, QGroupBox)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction
import logging
from scraper import main as scraper_main
import pandas as pd
import os
from datetime import datetime
from selenium import webdriver
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
import traceback
import platform
import time

class LogHandler(logging.Handler):
    def __init__(self, thread):
        super().__init__()
        self.thread = thread
        self.setFormatter(logging.Formatter('%(message)s'))  # Remove level prefix

    def emit(self, record):
        msg = self.format(record)
        self.thread.progress.emit(msg)

def setup_logging(log_dir='logs'):
    # Log dizinini oluştur
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Log dosyası adını oluştur (tarih ve saat ile)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = os.path.join(log_dir, f'scraper_{timestamp}.log')

    # Root logger'ı yapılandır
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # Önceki handler'ları temizle
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Dosya handler'ı
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Konsol handler'ı
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(message)s')  # Remove level prefix
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    return log_file

class ScraperThread(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)
    manual_confirmation_needed = pyqtSignal()
    total_ads_updated = pyqtSignal(int)
    current_ad_updated = pyqtSignal(int)
    progress_updated = pyqtSignal(int)
    page_progress_updated = pyqtSignal(int)

    def __init__(self, listing_type, sort_by, save_path, custom_filename):
        super().__init__()
        self.listing_type = listing_type
        self.sort_by = sort_by
        self.save_path = save_path
        self.custom_filename = custom_filename
        self.manual_confirmation = False
        self.driver = None
        self.is_paused = False
        self.should_stop = False
        self.should_start = False
        self.total_ads = 0
        self.current_ad = 0

    def pause(self):
        self.is_paused = True
        logging.info("Scraper duraklatıldı")

    def resume(self):
        self.is_paused = False
        logging.info("Scraper devam ediyor")

    def stop(self):
        self.should_stop = True
        logging.info("Scraper durduruluyor")

    def start_scraper(self):
        self.should_start = True
        logging.info("Scraper başlatılıyor...")

    def run(self):
        try:
            # Logging sistemini kur
            self.log_file = setup_logging()
            logging.info(f"Log dosyası oluşturuldu: {self.log_file}")

            # UI handler'ı ekle
            ui_handler = LogHandler(self)
            ui_handler.setLevel(logging.INFO)
            logging.getLogger().addHandler(ui_handler)

            # Chrome'u başlat
            logging.info("Chrome başlatılıyor...")
            options = webdriver.ChromeOptions()
            try:
                # Apple Silicon için özel ayarlar
                if sys.platform == 'darwin' and platform.machine() == 'arm64':
                    options.binary_location = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
                    self.driver = webdriver.Chrome(options=options)
                else:
                    # Diğer platformlar için webdriver-manager kullan
                    driver_path = ChromeDriverManager().install()
                    service = Service(driver_path)
                    self.driver = webdriver.Chrome(service=service, options=options)
                logging.info("✅ ChromeDriver başarıyla başlatıldı.")
            except Exception as e:
                logging.warning(f"ChromeDriver başlatılamadı: {e}")
                logging.info("PATH içindeki chromedriver kullanılacak, lütfen kurulu olduğundan emin olun.")
                self.driver = webdriver.Chrome(options=options)
            
            # Revy sayfasını aç
            self.driver.get("https://www.revy.com.tr/")
            logging.info("✅ Chrome başlatıldı ve Revy sayfası açıldı.")
            
            # Manuel onay sinyalini gönder
            self.manual_confirmation_needed.emit()
            
            # Manuel onay bekleniyor
            while not self.manual_confirmation:
                self.msleep(100)  # 100ms bekle
                if self.should_stop:
                    return
            
            # Başlatma sinyali bekleniyor
            logging.info("❗ Revy'ye giriş yaptıktan sonra 'Başlat' butonuna tıklayın...")
            while not self.should_start:
                self.msleep(100)  # 100ms bekle
                if self.should_stop:
                    return

            # FSBO sayfasına git
            logging.info("FSBO sayfasına yönlendiriliyor...")
            base_url = "https://www.revy.com.tr/app/portfoy/ilanlar?export=0&fsbo=true"
            if self.listing_type == "Yayından Kaldırılan İlanlar":
                base_url += "&tab=archive&area=my&advertisement_status=suspended"
            else:
                base_url += "&area=my&advertisement_status=active"
            
            # Sıralama parametresini ekle
            sort_params = {
                "Varsayılan sıralama (tarih ↓)": "date_desc",
                "Fiyat: Yüksekten düşüğe": "price_desc",
                "Fiyat: Düşükten yükseğe": "price_asc",
                "Tarih: Yeni>Eski": "date_desc",
                "Tarih: Eski>Yeni": "date_asc"
            }
            sort_param = sort_params.get(self.sort_by, "date_desc")
            url = f"{base_url}&sort={sort_param}"
            
            self.driver.get(url)
            
            # Sayfanın yüklenmesini bekle
            time.sleep(5)
            
            # Toplam ilan sayısını al
            try:
                total_ads_element = self.driver.find_element("id", "totalAdvertisement")
                self.total_ads = int(total_ads_element.text.replace(".", ""))
                self.total_ads_updated.emit(self.total_ads)
            except Exception as e:
                logging.warning(f"Toplam ilan sayısı alınamadı: {e}")
                self.total_ads = 0
            
            # Scraper'ı çalıştır
            logging.info("Scraper başlatılıyor...")
            scraper_main(
                listing_type=self.listing_type,
                sort_by=self.sort_by,
                save_path=self.save_path,
                custom_filename=self.custom_filename,
                thread=self
            )
            
            self.finished.emit()
        except Exception as e:
            error_msg = f"Scraper hatası: {str(e)}\n{traceback.format_exc()}"
            logging.error(error_msg)
            self.error.emit(error_msg)
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
            logging.info("Scraper işlemi sonlandı")

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("iScrape - Revy İlan Yönetimi")
        self.setMinimumSize(1000, 700)
        
        # Tam ekran ve borderless ayarları
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.showMaximized()
        
        # Koyu mod için stil
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e1e;
                color: #ffffff;
            }
            QGroupBox {
                border: 1px solid #3d3d3d;
                border-radius: 5px;
                margin-top: 1em;
                padding-top: 10px;
            }
            QGroupBox::title {
                color: #ffffff;
            }
            QLabel {
                color: #ffffff;
            }
            QPushButton {
                background-color: #0d6efd;
                color: white;
                border: none;
                padding: 5px 15px;
                border-radius: 3px;
            }
            QPushButton:hover {
                background-color: #0b5ed7;
            }
            QPushButton:disabled {
                background-color: #6c757d;
            }
            QComboBox, QLineEdit {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                padding: 5px;
                border-radius: 3px;
            }
            QProgressBar {
                border: 1px solid #3d3d3d;
                border-radius: 3px;
                text-align: center;
                background-color: #2d2d2d;
            }
            QProgressBar::chunk {
                background-color: #0d6efd;
            }
            QTextEdit {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 3px;
            }
        """)
        
        # Ana widget ve layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        
        # İlan Türü ve Sıralama
        settings_group = QGroupBox("Ayarlar")
        settings_layout = QVBoxLayout()
        
        # İlan Türü
        listing_layout = QHBoxLayout()
        listing_label = QLabel("İlan Türü:")
        self.listing_type = QComboBox()
        self.listing_type.addItems([
            "Yayındaki İlanlar",
            "Yayından Kaldırılan İlanlar"
        ])
        listing_layout.addWidget(listing_label)
        listing_layout.addWidget(self.listing_type)
        settings_layout.addLayout(listing_layout)
        
        # Sıralama
        sort_layout = QHBoxLayout()
        sort_label = QLabel("Sıralama:")
        self.sort_by = QComboBox()
        self.sort_by.addItems([
            "Varsayılan sıralama (tarih ↓)",
            "Fiyat: Yüksekten düşüğe",
            "Fiyat: Düşükten yükseğe",
            "Tarih: Yeni>Eski",
            "Tarih: Eski>Yeni"
        ])
        sort_layout.addWidget(sort_label)
        sort_layout.addWidget(self.sort_by)
        settings_layout.addLayout(sort_layout)
        
        # Kayıt Konumu
        save_layout = QHBoxLayout()
        self.save_path_label = QLabel("Kayıt Konumu:")
        self.save_path = QLineEdit()
        self.save_path.setReadOnly(True)
        self.browse_button = QPushButton("Gözat...")
        self.browse_button.clicked.connect(self.select_save_path)
        self.browse_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_button.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        save_layout.addWidget(self.save_path_label)
        save_layout.addWidget(self.save_path)
        save_layout.addWidget(self.browse_button)
        settings_layout.addLayout(save_layout)
        
        # Dosya Adı
        filename_layout = QHBoxLayout()
        self.filename_label = QLabel("Dosya Adı:")
        self.filename_input = QLineEdit()
        self.filename_input.setPlaceholderText("Örn: revy_ilanlar_2024")
        filename_layout.addWidget(self.filename_label)
        filename_layout.addWidget(self.filename_input)
        settings_layout.addLayout(filename_layout)
        
        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)
        
        # İlerleme Çubuğu
        progress_group = QGroupBox("İlerleme")
        progress_layout = QVBoxLayout()
        
        self.progress_label = QLabel("0/0 ilan işlendi")
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar)
        progress_group.setLayout(progress_layout)
        layout.addWidget(progress_group)
        
        # Kontrol Butonları
        control_layout = QHBoxLayout()
        self.start_button = QPushButton("Chrome'u Aç ve Giriş Yap")
        self.start_button.clicked.connect(self.start_scraper)
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        
        self.pause_button = QPushButton("Duraklat")
        self.pause_button.clicked.connect(self.pause_scraper)
        self.pause_button.setEnabled(False)
        self.pause_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pause_button.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        
        self.stop_button = QPushButton("Durdur")
        self.stop_button.clicked.connect(self.stop_scraper)
        self.stop_button.setEnabled(False)
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #d32f2f;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        
        self.continue_button = QPushButton("Başlat")
        self.continue_button.clicked.connect(self.continue_scraper)
        self.continue_button.setEnabled(False)
        self.continue_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.continue_button.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:disabled {
                background-color: #cccccc;
            }
        """)
        
        control_layout.addWidget(self.start_button)
        control_layout.addWidget(self.pause_button)
        control_layout.addWidget(self.stop_button)
        control_layout.addWidget(self.continue_button)
        layout.addLayout(control_layout)
        
        # Log Alanı
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        layout.addWidget(self.log_area)
        
        self.scraper_thread = None

    def select_save_path(self):
        file_path = QFileDialog.getExistingDirectory(
            self,
            "Kayıt Konumu Seç",
            "",
            QFileDialog.Option.ShowDirsOnly
        )
        if file_path:
            self.save_path.setText(file_path)

    def start_scraper(self):
        if not self.save_path.text():
            QMessageBox.warning(self, "Uyarı", "Lütfen bir kayıt konumu seçin!")
            return
            
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.continue_button.setEnabled(False)
        self.log_area.clear()
        
        # Dosya adını al
        custom_filename = self.filename_input.text().strip()
        if not custom_filename:
            custom_filename = "revy_ilanlar"
        
        self.scraper_thread = ScraperThread(
            self.listing_type.currentText(),
            self.sort_by.currentText(),
            self.save_path.text(),
            custom_filename
        )
        self.scraper_thread.progress.connect(self.update_log)
        self.scraper_thread.finished.connect(self.scraper_finished)
        self.scraper_thread.error.connect(self.scraper_error)
        self.scraper_thread.manual_confirmation_needed.connect(self.show_manual_confirmation_dialog)
        self.scraper_thread.total_ads_updated.connect(self.update_total_ads)
        self.scraper_thread.current_ad_updated.connect(self.update_current_ad)
        self.scraper_thread.progress_updated.connect(self.update_current_ad)
        self.scraper_thread.page_progress_updated.connect(self.update_page_progress)
        self.scraper_thread.start()

    def pause_scraper(self):
        if not self.scraper_thread or not self.scraper_thread.isRunning():
            return
            
        if self.scraper_thread.is_paused:
            self.scraper_thread.resume()
            self.pause_button.setText("Duraklat")
        else:
            self.scraper_thread.pause()
            self.pause_button.setText("Devam Et")

    def stop_scraper(self):
        if not self.scraper_thread or not self.scraper_thread.isRunning():
            return
            
        reply = QMessageBox.question(
            self, 'Durdurma Onayı',
            'Scraper işlemini durdurmak istediğinizden emin misiniz?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self.scraper_thread.stop()

    def continue_scraper(self):
        if self.scraper_thread and self.scraper_thread.isRunning():
            self.scraper_thread.start_scraper()
            self.continue_button.setEnabled(False)

    def show_manual_confirmation_dialog(self):
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText("Lütfen Revy'ye giriş yapın.")
        msg.setInformativeText("Revy'e giriş yaptıktan sonra 'Tamam' butonuna tıklayın.")
        msg.setWindowTitle("Manuel Onay Gerekli")
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        
        if msg.exec() == QMessageBox.StandardButton.Ok:
            self.scraper_thread.manual_confirmation = True
            self.continue_button.setEnabled(True)

    def scraper_finished(self):
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.continue_button.setEnabled(False)
        QMessageBox.information(self, "Başarılı", "Scraper işlemi tamamlandı!")

    def scraper_error(self, error_message):
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.continue_button.setEnabled(False)
        QMessageBox.critical(self, "Hata", f"Scraper sırasında hata oluştu:\n{error_message}")

    def update_log(self, message):
        self.log_area.append(message)
        # Otomatik scroll
        self.log_area.verticalScrollBar().setValue(
            self.log_area.verticalScrollBar().maximum()
        )

    def update_total_ads(self, total):
        self.progress_bar.setMaximum(total)
        self.update_progress_label()

    def update_current_ad(self, current):
        self.progress_bar.setValue(current)
        self.update_progress_label()

    def update_progress_label(self):
        current = self.progress_bar.value()
        total = self.progress_bar.maximum()
        self.progress_label.setText(f"{current}/{total} ilan işlendi")

    def update_page_progress(self, page):
        self.progress_label.setText(f"Sayfa {page} işleniyor...")

    def show_usage_instructions(self):
        instructions = """
iScrape - Revy İlan Yönetimi Kullanım Talimatları

1. Uygulama Başlatma:
   - Uygulamayı başlatmak için terminal/komut isteminde 'python main.py' komutunu çalıştırın
   - Uygulama başladığında otomatik olarak Chrome tarayıcısı açılacaktır

2. İlan Çekme İşlemi:
   a) Ayarlar:
      - İlan Türü: "Yayındaki İlanlar" veya "Yayından Kaldırılan İlanlar" seçin
      - Sıralama: İlanları nasıl sıralamak istediğinizi seçin
      - Kayıt Konumu: CSV dosyasının kaydedileceği klasörü seçin
      - Dosya Adı: İsteğe bağlı olarak özel bir dosya adı girin
         * Boş bırakırsanız otomatik olarak "revy_ilanlar" adı kullanılır
         * Girilen adın sonuna otomatik olarak tarih ve saat eklenecektir

   b) Chrome'u Aç ve Giriş Yap:
      - "Chrome'u Aç ve Giriş Yap" butonuna tıklayın
      - Chrome tarayıcısı açılacak ve Revy sayfasına yönlendirileceksiniz
      - Revy hesabınıza giriş yapın
      - Giriş yaptıktan sonra "Tamam" butonuna tıklayın
      - "Başlat" butonuna tıklayın

   c) Veri Çekme:
      - Otomatik olarak FSBO sayfasına gidilecek
      - İlanlar otomatik olarak çekilmeye başlayacak
      - İlerleme çubuğundan işlemin durumunu takip edebilirsiniz
      - Her ilan çekildiğinde log ekranında "✅ Veri Çekildi" mesajı görünecek

3. Kontrol Butonları:
   - Duraklat: İşlemi geçici olarak durdurur
   - Devam Et: Duraklatılan işlemi devam ettirir
   - Durdur: İşlemi tamamen sonlandırır
      * Durdurma işlemi onay gerektirir
      * Durdurulduğunda o ana kadar çekilen veriler kaydedilir

4. WhatsApp Bot Kullanımı:
   a) Bot Başlatma:
      - WhatsApp Bot sekmesine geçin
      - CSV dosyasını seçin (scraper'dan çekilen veriler)
      - Test modu için telefon numaranızı girin
      - Mesaj şablonlarını seçin
      - "Chrome'u Aç ve Giriş Yap" butonuna tıklayın
      - WhatsApp Web'e QR kod ile giriş yapın
      - Giriş yaptıktan sonra "Tamam" butonuna tıklayın
      - "Başlat" butonuna tıklayın

5. Hata Durumları:
   - Chrome başlatılamadığında: Chrome'un yüklü olduğundan emin olun
   - Giriş yapılamadığında: Revy hesap bilgilerinizi kontrol edin
   - İlan çekilemediğinde: İnternet bağlantınızı kontrol edin
   - Dosya kaydedilemediğinde: Disk alanınızı ve klasör izinlerinizi kontrol edin

6. Önemli Notlar:
   - İşlem sırasında Chrome penceresini kapatmayın
   - İnternet bağlantınızın stabil olduğundan emin olun
   - Yeterli disk alanı olduğundan emin olun
   - İşlem durdurulduğunda o ana kadar çekilen veriler kaydedilir
   - CSV dosyası UTF-8 formatında kaydedilir, Excel'de açarken karakter kodlamasına dikkat edin
   - WhatsApp bot kullanırken telefonunuzun internete bağlı olduğundan emin olun
"""
        dlg = QDialog(self)
        dlg.setWindowTitle("Kullanım Talimatları")
        
        # Ana pencerenin boyutlarını al
        main_size = self.size()
        
        # Dialog boyutunu ana pencereden biraz daha küçük yap
        dialog_width = int(main_size.width() * 0.8)  # Ana pencerenin %80'i
        dialog_height = int(main_size.height() * 0.8)  # Ana pencerenin %80'i
        
        # Minimum ve maksimum boyutları ayarla
        dlg.setMinimumSize(dialog_width, dialog_height)
        dlg.setMaximumSize(dialog_width, dialog_height)
        
        layout = QVBoxLayout(dlg)
        
        # Metin alanı için scroll bar ekle ve boyutlandır
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(instructions)
        text.setStyleSheet("""
            QTextEdit {
                background-color: #2d2d2d;
                color: #ffffff;
                border: 1px solid #3d3d3d;
                border-radius: 3px;
                padding: 10px;
                font-size: 13px;
                line-height: 1.5;
            }
        """)
        
        # Metin alanının boyutunu ayarla
        text.setMinimumHeight(int(dialog_height * 0.9))  # Dialog yüksekliğinin %90'ı
        
        layout.addWidget(text)
        
        # Kapat butonu
        btn = QPushButton("Kapat")
        btn.clicked.connect(dlg.accept)
        btn.setStyleSheet("""
            QPushButton {
                background-color: #0d6efd;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #0b5ed7;
            }
        """)
        
        # Buton için yatay hizalama
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(btn)
        btn_layout.addStretch()
        
        layout.addLayout(btn_layout)
        dlg.exec()

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main() 