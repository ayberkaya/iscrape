import sys
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                            QHBoxLayout, QPushButton, QLabel, QComboBox, 
                            QProgressBar, QTextEdit, QMessageBox, QFileDialog, 
                            QDialog, QTabWidget, QCheckBox, QLineEdit, QGroupBox, QSizePolicy)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction
import logging
from scraper import main as scraper_main
from scraper_ui import MainWindow as ScraperWindow
from autobot import main as whatsapp_main
import pandas as pd
import os
from datetime import datetime
from selenium import webdriver
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
import traceback
import platform
from openai import OpenAI
import json
from config import OPENAI_API_KEY

class AITemplateGenerator(QThread):
    templates_ready = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, prompt, api_key):
        super().__init__()
        self.prompt = prompt
        self.api_key = api_key

    def run(self):
        try:
            client = OpenAI(api_key=self.api_key)
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": """Sen bir emlak danışmanısın. FSBO (For Sale By Owner) ilanları için WhatsApp mesajları oluşturuyorsun. 
                    
Amacın: Mülk sahiplerini, mülklerinin satış/kiralama sürecini size devretmeye ikna etmek.

Şablon oluştururken şu kurallara dikkat et:
1. Her şablonda {title} değişkeni kullanılmalı ve bu değişken kalın ve tırnak içinde olmalı (*\"{title}\"*)
2. Mesajlar kısa, öz ve ikna edici olmalı
3. İstenen yazı tonuna uygun bir dil kullan (örn: samimi, profesyonel, arkadaş canlısı, ciddi)
4. Mülk sahibinin işini kolaylaştıracağını vurgula
5. Güven oluştur ve deneyimini göster
6. CTA (Call to Action) içermeli (görüşme, arama, mesaj atma gibi)
7. Maksimum 2-3 cümle olmalı
8. Mülk sahibinin zaman ve emek tasarrufu sağlayacağını belirt
9. Potansiyel alıcı/kiracı portföyünüzden bahset
10. Sürecin hızlanacağını vurgula"""},
                    {"role": "user", "content": f"Lütfen aşağıdaki yazı tonuna uygun 3 farklı WhatsApp mesaj şablonu oluştur. Bu şablonlar FSBO ilan sahiplerini, mülklerinin satış/kiralama sürecini bize devretmeye ikna etmeli. Yazı tonu: {self.prompt}"}
                ],
                temperature=0.7,
                n=3
            )
            # Tüm şablonları tek bir listede topla
            all_templates = []
            for choice in response.choices:
                # Her bir choice.message.content'i satırlara/paragraflara böl
                lines = [line.strip() for line in choice.message.content.split('\n') if line.strip()]
                for line in lines:
                    # 1. ... 2. ... 3. ... gibi satırları ayıkla
                    if line[0].isdigit() and line[1] == '.':
                        template = line[line.find('.')+1:].strip()
                        if "{title}" in template and len(template) > 20:
                            all_templates.append(template)
                    elif "{title}" in line and len(line) > 20:
                        all_templates.append(line)
                if len(all_templates) >= 3:
                    break
            self.templates_ready.emit(all_templates[:3])
        except Exception as e:
            error_message = str(e)
            if "insufficient_quota" in error_message:
                self.error.emit(
                    "API kotanız aşıldı veya ücretsiz deneme süreniz doldu.\n\n"
                    "Çözüm önerileri:\n"
                    "1. OpenAI hesabınıza kredi ekleyin\n"
                    "2. Yeni bir API anahtarı oluşturun\n"
                    "3. Varsayılan şablonları kullanın\n\n"
                    "Daha fazla bilgi için: https://platform.openai.com/account/billing"
                )
            else:
                self.error.emit(f"AI şablon oluşturma hatası: {error_message}")

class LogHandler(logging.Handler):
    def __init__(self, thread):
        super().__init__()
        self.thread = thread
        self.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))

    def emit(self, record):
        msg = self.format(record)
        self.thread.progress.emit(msg)

def setup_logging(log_dir='logs'):
    # Log dizinini oluştur
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    # Log dosyası adını oluştur (tarih ve saat ile)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_file = os.path.join(log_dir, f'whatsapp_bot_{timestamp}.log')

    # Root logger'ı yapılandır
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Dosya handler'ı
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Konsol handler'ı
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    return log_file

class WhatsAppBotThread(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)
    manual_confirmation_needed = pyqtSignal()

    def __init__(self, csv_file, test_mode=False, test_phone="", selected_templates=None, custom_template=None):
        super().__init__()
        self.csv_file = csv_file
        self.test_mode = test_mode
        self.test_phone = test_phone
        self.selected_templates = selected_templates
        self.custom_template = custom_template
        self.manual_confirmation = False
        self.driver = None
        self.is_paused = False
        self.should_stop = False
        self.should_start = False

    def pause(self):
        self.is_paused = True
        logging.info("WhatsApp bot duraklatıldı")

    def resume(self):
        self.is_paused = False
        logging.info("WhatsApp bot devam ediyor")

    def stop(self):
        self.should_stop = True
        logging.info("WhatsApp bot durduruluyor")

    def start_bot(self):
        self.should_start = True
        logging.info("WhatsApp bot başlatılıyor...")

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
            
            # WhatsApp Web sayfasını aç
            self.driver.get("https://web.whatsapp.com")
            logging.info("✅ Chrome başlatıldı ve WhatsApp Web sayfası açıldı.")
            
            # Manuel onay sinyalini gönder
            self.manual_confirmation_needed.emit()
            
            # Manuel onay bekleniyor
            while not self.manual_confirmation:
                self.msleep(100)  # 100ms bekle
                if self.should_stop:
                    return
            
            # Başlatma sinyali bekleniyor
            logging.info("❗ WhatsApp'a giriş yaptıktan sonra 'Başlat' butonuna tıklayın...")
            while not self.should_start:
                self.msleep(100)  # 100ms bekle
                if self.should_stop:
                    return
            
            # Bot'u çalıştır
            logging.info("WhatsApp bot başlatılıyor...")
            whatsapp_main(
                csv_file=self.csv_file,
                thread=self,
                test_mode=self.test_mode,
                test_phone=self.test_phone,
                selected_templates=self.selected_templates,
                custom_template=self.custom_template
            )
            
            self.finished.emit()
        except Exception as e:
            error_msg = f"WhatsApp bot hatası: {str(e)}\n{traceback.format_exc()}"
            logging.error(error_msg)
            self.error.emit(error_msg)
        finally:
            if self.driver:
                try:
                    self.driver.quit()
                except:
                    pass
            logging.info("WhatsApp bot işlemi sonlandı")

class WhatsAppBotTab(QWidget):
    def __init__(self):
        super().__init__()
        self.api_key = OPENAI_API_KEY
        self.template_contents = {
            "SATILIK": {
                "template1": (
                    "Merhaba, *\"{title}\"* ilanınızı gördüm. Mülkünüzü hızlıca {islem} için geniş portföyümle yardımcı olabilirim. Detayları konuşmak ister misiniz?"
                ),
                "template2": (
                    "Selam, *\"{title}\"* ilanınız dikkatimi çekti. Size zaman ve emek tasarrufu sağlayacak şekilde {islem} sürecinizi kolaylaştırabilirim. Görüşmek ister misiniz?"
                ),
                "template3": (
                    "Merhaba, *\"{title}\"* ilanınız için potansiyel müşteri portföyüm hazır. {islem} sürecini hızlandırmak için iletişime geçelim mi?"
                )
            },
            "KIRALIK": {
                "template1": (
                    "Merhaba, *\"{title}\"* ilanınızı gördüm. Mülkünüzü hızlıca {islem} için geniş portföyümle yardımcı olabilirim. Detayları konuşmak ister misiniz?"
                ),
                "template2": (
                    "Selam, *\"{title}\"* ilanınız dikkatimi çekti. Size zaman ve emek tasarrufu sağlayacak şekilde {islem} sürecinizi kolaylaştırabilirim. Görüşmek ister misiniz?"
                ),
                "template3": (
                    "Merhaba, *\"{title}\"* ilanınız için potansiyel müşteri portföyüm hazır. {islem} sürecini hızlandırmak için iletişime geçelim mi?"
                )
            }
        }
        self.ai_generated = {"SATILIK": [], "KIRALIK": []}  # AI şablonları ayrı tutulacak
        self.bot_thread = None
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()
        
        # Üst bölüm için yatay layout
        top_layout = QHBoxLayout()
        top_layout.setSpacing(8)  # Daha az boşluk için spacing'i küçült
        
        # Sol üst bölüm (CSV ve Test Ayarları)
        left_group = QGroupBox("Dosya ve Test Ayarları")
        left_group.setMaximumWidth(340)
        left_group.setMinimumHeight(0)
        left_group.setMaximumHeight(150)
        left_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        left_layout = QVBoxLayout()
        left_layout.setSpacing(8)
        left_layout.setContentsMargins(4, 2, 4, 2)

        # Çıktının Kaydedileceği Konum
        data_location_layout = QHBoxLayout()
        self.data_location_label = QLabel("Veri Dosyası Konumu:")
        self.data_location = QLineEdit()
        self.data_location.setReadOnly(True)
        self.data_location.setMinimumHeight(18)
        self.data_location.setMaximumHeight(22)
        self.data_location.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.data_location_browse = QPushButton("Gözat...")
        self.data_location_browse.setMinimumHeight(18)
        self.data_location_browse.setMaximumHeight(22)
        self.data_location_browse.setMaximumWidth(60)
        self.data_location_browse.clicked.connect(self.select_data_file)
        data_location_layout.addWidget(self.data_location_label)
        data_location_layout.addWidget(self.data_location)
        data_location_layout.addWidget(self.data_location_browse)
        left_layout.addLayout(data_location_layout)

        # Test Modu
        self.test_mode_checkbox = QCheckBox("Test Modu")
        self.test_mode_checkbox.setChecked(True)
        self.test_mode_checkbox.setMinimumHeight(16)
        self.test_mode_checkbox.setMaximumHeight(20)
        left_layout.addWidget(self.test_mode_checkbox)

        # Telefon numarası uyarısı
        phone_warning = QLabel("Telefon numaranızı başında 0 olmadan giriniz")
        phone_warning.setStyleSheet("color: #666; font-size: 11px;")
        phone_warning.setMinimumHeight(13)
        phone_warning.setMaximumHeight(16)
        left_layout.addWidget(phone_warning)

        # Test Telefon Numarası Girişi
        self.test_phone_input = QLineEdit()
        self.test_phone_input.setPlaceholderText("5xx... şeklinde giriniz")
        self.test_phone_input.setMinimumHeight(18)
        self.test_phone_input.setMaximumHeight(22)
        self.test_phone_input.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        left_layout.addWidget(self.test_phone_input)

        left_group.setLayout(left_layout)
        # Yatayda özel şablon kutusuyla üst kenarı hizalı olacak şekilde hizala
        top_layout.addWidget(left_group, alignment=Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        
        # Sağ üst bölüm (Özel Şablon)
        right_group = QGroupBox("Özel Şablon")
        right_group.setMinimumWidth(500)
        right_group.setMaximumWidth(900)
        right_layout = QVBoxLayout()
        
        # Özel şablon checkbox'ı
        custom_checkbox_layout = QHBoxLayout()
        self.custom_template_checkbox = QCheckBox("Özel şablon kullan")
        self.custom_template_checkbox.setChecked(False)
        self.custom_template_checkbox.stateChanged.connect(self.toggle_custom_template)
        custom_checkbox_layout.addWidget(self.custom_template_checkbox)
        right_layout.addLayout(custom_checkbox_layout)
        
        # AI Şablon Oluşturma
        ai_layout = QHBoxLayout()
        self.ai_prompt = QLineEdit()
        self.ai_prompt.setPlaceholderText("Yazı tonu girin (örn: samimi, profesyonel, arkadaş canlısı, ciddi)")
        self.ai_generate_button = QPushButton("AI ile Şablon Oluştur")
        self.ai_generate_button.clicked.connect(self.generate_ai_templates)
        ai_layout.addWidget(self.ai_prompt)
        ai_layout.addWidget(self.ai_generate_button)
        right_layout.addLayout(ai_layout)
        
        # Title sabiti checkbox'ı
        title_checkbox_layout = QHBoxLayout()
        self.title_checkbox = QCheckBox("İlan başlığını ekle ({title})")
        self.title_checkbox.setChecked(False)
        self.title_checkbox.stateChanged.connect(self.update_custom_template)
        title_checkbox_layout.addWidget(self.title_checkbox)
        right_layout.addLayout(title_checkbox_layout)
        
        # Tek özel şablon metin alanı
        self.custom_template = QTextEdit()
        self.custom_template.setPlaceholderText("Mesaj şablonunuzu buraya yazın veya AI ile oluşturun...")
        self.custom_template.setMinimumWidth(400)
        self.custom_template.setMaximumHeight(150)
        self.custom_template.setStyleSheet("""
            QTextEdit {
                background-color: #f8f9fa;
                color: #212529;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 8px;
                font-size: 13px;
            }
        """)
        right_layout.addWidget(self.custom_template)
        
        right_group.setLayout(right_layout)
        top_layout.addWidget(right_group)
        
        # Üst bölümü ana layout'a ekle
        layout.addLayout(top_layout)
        
        # Mesaj Şablonları
        template_group = QGroupBox("Hazır Mesaj Şablonları")
        template_layout = QVBoxLayout()

        # Genel Hazır Şablonlar
        self.general_templates = []
        general_templates_layout = QVBoxLayout()
        general_templates_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        for i, (key, content) in enumerate(self.template_contents["SATILIK"].items(), 1):
            checkbox = QCheckBox(f"Şablon {i}")
            checkbox.setChecked(False)
            checkbox.stateChanged.connect(lambda state, idx=i: self.update_template_content("GENEL", idx))
            self.general_templates.append(checkbox)
            general_templates_layout.addWidget(checkbox)
        self.general_content = QTextEdit()
        self.general_content.setReadOnly(True)
        self.general_content.setMaximumHeight(200)
        self.general_content.setStyleSheet("""
            QTextEdit {
                background-color: #f8f9fa;
                color: #212529;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 8px;
                font-size: 13px;
            }
        """)
        self.general_content.setVisible(False)
        general_templates_layout.addWidget(self.general_content)
        template_layout.addLayout(general_templates_layout)
        template_group.setLayout(template_layout)
        layout.addWidget(template_group)
        
        # Kontrol Butonları
        control_layout = QHBoxLayout()
        self.start_button = QPushButton("Chrome'u Aç ve Giriş Yap")
        self.start_button.clicked.connect(self.start_bot)
        self.pause_button = QPushButton("Duraklat")
        self.pause_button.clicked.connect(self.pause_bot)
        self.pause_button.setEnabled(False)
        self.stop_button = QPushButton("Durdur")
        self.stop_button.clicked.connect(self.stop_bot)
        self.stop_button.setEnabled(False)
        self.continue_button = QPushButton("Başlat")
        self.continue_button.clicked.connect(self.continue_bot)
        self.continue_button.setEnabled(False)
        control_layout.addWidget(self.start_button)
        control_layout.addWidget(self.pause_button)
        control_layout.addWidget(self.stop_button)
        control_layout.addWidget(self.continue_button)
        layout.addLayout(control_layout)
        
        # Log Alanı
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        layout.addWidget(self.log_area)
        
        self.setLayout(layout)
        
        # İlk şablonları göster
        self.update_template_content("GENEL", 1)

    def update_template_content(self, template_type, template_num):
        # Tüm seçili genel şablonları göster
        selected_templates = []
        for i, checkbox in enumerate(self.general_templates, 1):
            if checkbox.isChecked():
                template_key = f"template{i}"
                content = self.template_contents["SATILIK"][template_key]
                selected_templates.append(f"Şablon {i}:\n{content}\n")
        if selected_templates:
            self.general_content.setText("\n".join(selected_templates))
            self.general_content.setVisible(True)
        else:
            self.general_content.setVisible(False)

    def toggle_custom_template(self, state):
        # Hazır şablonları aktif/pasif yap
        template_group = self.findChild(QGroupBox, "Hazır Mesaj Şablonları")
        if template_group:
            template_group.setEnabled(not state)
            
        # Hazır şablonların checkbox'larını devre dışı bırak
        for checkbox in self.general_templates:
            checkbox.setEnabled(not state)
            if state:  # Özel şablon seçiliyse checkbox'ları işaretsiz yap
                checkbox.setChecked(False)
            
        # Özel şablon alanını aktif/pasif yap
        self.custom_template.setEnabled(state)
        self.title_checkbox.setEnabled(state)
        
        # Eğer özel şablon kapatılıyorsa, title checkbox'ını da kapat
        if not state:
            self.title_checkbox.setChecked(False)
            self.custom_template.clear()

    def get_selected_templates(self):
        # Sadece özel şablon kutusunu kullan
        if self.custom_template_checkbox.isChecked():
            custom_template = self.custom_template.toPlainText().strip()
            if custom_template:
                return {"SATILIK": ["custom"], "KIRALIK": ["custom"]}, custom_template, self.ai_generated
        # Hazır şablonları kontrol et
        selected = {"SATILIK": [], "KIRALIK": []}
        for i, checkbox in enumerate(self.general_templates, 1):
            if checkbox.isChecked():
                selected["SATILIK"].append(f"template{i}")
                selected["KIRALIK"].append(f"template{i}")
        return selected, None, self.ai_generated

    def select_data_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Veri Dosyasını Seç", "", "CSV Dosyaları (*.csv);;Tüm Dosyalar (*.*)")
        if file_path:
            self.data_location.setText(file_path)

    def start_bot(self):
        if not self.data_location.text():
            QMessageBox.warning(self, "Uyarı", "Lütfen veri dosyası konumunu seçin!")
            return
            
        if self.test_mode_checkbox.isChecked():
            test_phone = self.test_phone_input.text().strip()
            if not test_phone:
                QMessageBox.warning(self, "Uyarı", "Test modu açıkken test telefon numarası girmelisiniz!")
                return
                
            # Telefon numarasını formatla
            if test_phone.startswith("0"):
                test_phone = test_phone[1:]
            if test_phone.startswith("+90"):
                test_phone = test_phone[3:]
            if not test_phone.startswith("5"):
                QMessageBox.warning(self, "Uyarı", "Telefon numarası 5 ile başlamalıdır!")
                return
                
            # +90 önekini ekle
            formatted_phone = f"+90{test_phone}"
            self.test_phone_input.setText(test_phone)  # Kullanıcıya sadece numarayı göster
            
        # En az bir şablon seçili olmalı
        selected_templates, custom_template, ai_generated = self.get_selected_templates()
        if not any(selected_templates.values()):
            QMessageBox.warning(self, "Uyarı", "Lütfen en az bir mesaj şablonu seçin!")
            return
            
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.continue_button.setEnabled(False)
        self.log_area.clear()
        
        self.bot_thread = WhatsAppBotThread(
            self.data_location.text(),
            self.test_mode_checkbox.isChecked(),
            formatted_phone,
            selected_templates,
            custom_template
        )
        self.bot_thread.progress.connect(self.update_log)
        self.bot_thread.finished.connect(self.bot_finished)
        self.bot_thread.error.connect(self.bot_error)
        self.bot_thread.manual_confirmation_needed.connect(self.show_manual_confirmation_dialog)
        self.bot_thread.start()

    def pause_bot(self):
        if not self.bot_thread or not self.bot_thread.isRunning():
            return
            
        if self.bot_thread.is_paused:
            self.bot_thread.resume()
            self.pause_button.setText("Duraklat")
        else:
            self.bot_thread.pause()
            self.pause_button.setText("Devam Et")

    def stop_bot(self):
        if not self.bot_thread or not self.bot_thread.isRunning():
            return
            
        reply = QMessageBox.question(
            self, 'Durdurma Onayı',
            'WhatsApp bot işlemini durdurmak istediğinizden emin misiniz?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self.bot_thread.stop()

    def continue_bot(self):
        if self.bot_thread and self.bot_thread.isRunning():
            self.bot_thread.start_bot()
            self.continue_button.setEnabled(False)

    def show_manual_confirmation_dialog(self):
        msg = QMessageBox()
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setText("Lütfen WhatsApp Web'e QR kod ile giriş yapın.")
        msg.setInformativeText("Giriş yaptıktan sonra 'Tamam' butonuna tıklayın.")
        msg.setWindowTitle("Manuel Onay Gerekli")
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        
        if msg.exec() == QMessageBox.StandardButton.Ok:
            self.bot_thread.manual_confirmation = True
            self.continue_button.setEnabled(True)

    def bot_finished(self):
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.continue_button.setEnabled(False)
        QMessageBox.information(self, "Başarılı", "WhatsApp bot işlemi tamamlandı!")

    def bot_error(self, error_message):
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.continue_button.setEnabled(False)
        QMessageBox.critical(self, "Hata", f"WhatsApp bot sırasında hata oluştu:\n{error_message}")

    def update_log(self, message):
        self.log_area.append(message)
        # Otomatik scroll
        self.log_area.verticalScrollBar().setValue(
            self.log_area.verticalScrollBar().maximum()
        )

    def update_custom_template(self, state):
        if state == Qt.CheckState.Checked.value:
            # Title sabitini WhatsApp formatında ekle
            current_text = self.custom_template.toPlainText()
            formatted_title = "*\"{title}\"*"  # WhatsApp formatı: kalın ve tırnak içinde
            if formatted_title not in current_text:
                # Eğer metin varsa, ortaya ekle
                if current_text:
                    words = current_text.split()
                    mid = len(words) // 2
                    words.insert(mid, formatted_title)
                    self.custom_template.setText(" ".join(words))
                else:
                    self.custom_template.setText(formatted_title)
        else:
            # Title sabitini kaldır
            current_text = self.custom_template.toPlainText()
            self.custom_template.setText(current_text.replace("*\"{title}\"*", ""))

    def generate_ai_templates(self):
        prompt = self.ai_prompt.text().strip()
        if not prompt:
            QMessageBox.warning(self, "Uyarı", "Lütfen şablon için anahtar kelimeler girin!")
            return
        self.ai_generate_button.setEnabled(False)
        self.ai_generate_button.setText("Oluşturuluyor...")
        self.ai_generator = AITemplateGenerator(prompt, self.api_key)
        self.ai_generator.templates_ready.connect(self.show_ai_templates)
        self.ai_generator.error.connect(self.show_ai_error)
        self.ai_generator.start()

    def show_ai_templates(self, templates):
        # Kategori ayrımı kaldırıldı, sadece SATILIK'a ekle
        self.ai_generated["SATILIK"].extend(templates[:3])
        current_text = self.custom_template.toPlainText().strip()
        if current_text:
            current_text += "\n\n"
        # Şablonlar arasına boş satır ekle
        current_text += "\n\n".join(templates[:3])
        self.custom_template.setText(current_text)
        QMessageBox.information(self, "AI Şablonları", f"{len(templates[:3])} yeni şablon eklendi.")
        self.ai_generate_button.setEnabled(True)
        self.ai_generate_button.setText("AI ile Şablon Oluştur")
        self.update_template_content("GENEL", 1)

    def show_ai_error(self, error_message):
        QMessageBox.critical(self, "API Hatası", error_message)
        self.ai_generate_button.setEnabled(True)
        self.ai_generate_button.setText("AI ile Şablon Oluştur")
        
        # Hata mesajında "insufficient_quota" varsa, özel şablon alanını devre dışı bırak
        if "insufficient_quota" in error_message:
            self.custom_template_checkbox.setChecked(False)
            self.custom_template_checkbox.setEnabled(False)
            self.ai_prompt.setEnabled(False)
            self.ai_generate_button.setEnabled(False)
            self.custom_template.setEnabled(False)
            self.title_checkbox.setEnabled(False)
            
            # Kullanıcıya bilgi mesajı göster
            QMessageBox.information(
                self,
                "Özellik Devre Dışı",
                "API kotanız aşıldığı için AI şablon oluşturma özelliği geçici olarak devre dışı bırakıldı.\n"
                "Lütfen hazır şablonları kullanın veya API kotanızı yenileyin."
            )

    def show_usage_instructions(self):
        QMessageBox.information(
            self,
            "WhatsApp Bot Yardım",
            "Veri dosyasını seçin, şablonunuzu oluşturun veya seçin ve başlat butonuna tıklayın.\n"
            "Test modu ile önce kendinize mesaj gönderebilirsiniz."
        )

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("iScrape - Revy İlan Yönetimi")
        self.setMinimumSize(1000, 700)
        
        # Tam ekran ve borderless ayarları
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.showMaximized()
        
        # Ana widget ve layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        
        # Tab widget
        self.tabs = QTabWidget()
        
        # Scraper tab
        self.scraper_tab = ScraperWindow()
        self.tabs.addTab(self.scraper_tab, "İlan Scraper")
        
        # WhatsApp Bot tab
        self.whatsapp_tab = WhatsAppBotTab()
        self.tabs.addTab(self.whatsapp_tab, "WhatsApp Bot")
        
        layout.addWidget(self.tabs)
        
        # Menü çubuğu
        menubar = self.menuBar()
        help_menu = menubar.addMenu("Yardım")
        about_action = QAction("Hakkında", self)
        about_action.triggered.connect(self.show_about_dialog)
        help_action = QAction("Kullanım Talimatı", self)
        help_action.triggered.connect(self.show_help_dialog)
        help_menu.addAction(help_action)
        help_menu.addAction(about_action)

    def show_about_dialog(self):
        html = ("<b>iScrape - Revy İlan Yönetimi</b><br><br>"
                "Geliştirici: Kaya<br>"
                "Sürüm: 1.0<br><br>"
                "Bu uygulama, Revy.com.tr üzerindeki FSBO ilanlarını çekmek ve "
                "WhatsApp üzerinden otomatik mesaj göndermek için geliştirilmiştir.<br>"
                "Tüm hakları saklıdır.")
        dlg = QDialog(self)
        dlg.setWindowTitle("Hakkında")
        dlg.setMinimumWidth(400)
        layout = QVBoxLayout(dlg)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setHtml(html)
        text.setStyleSheet("background: #fff; color: #222; font-size: 14px; border: none;")
        layout.addWidget(text)
        btn = QPushButton("Kapat")
        btn.clicked.connect(dlg.accept)
        btn.setStyleSheet("background: #2196F3; color: white; padding: 6px 16px; border-radius: 4px;")
        layout.addWidget(btn)
        dlg.exec()

    def show_help_dialog(self):
        """Yardım menüsünü göster"""
        if self.tabs.currentWidget() == self.scraper_tab:
            self.scraper_tab.show_usage_instructions()
        elif self.tabs.currentWidget() == self.whatsapp_tab:
            self.whatsapp_tab.show_usage_instructions()
        else:
            QMessageBox.information(self, "Yardım", "Bu sekme için yardım bulunmamaktadır.")

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main() 