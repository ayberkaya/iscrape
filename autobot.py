import pandas as pd
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager
import urllib.parse
import logging
import sys
import platform
import os
import random

# --- Configuration ---
DEFAULT_CSV_FILE = "ilanlar.csv"

# --- Message Templates ---
MESSAGE_TEMPLATES = {
    "SATILIK": {
        "template1": (
            "Merhaba, ilanınız *\"{title}\"* satışa sunduğunuz bu mülk için alıcı portföyümüze eklenebilir. "
            "Süreci hızlandırmak isterseniz yardımcı olabilirim."
        ),
        "template2": (
            "Merhaba, *\"{title}\"* ilanınızı inceledim. "
            "Mülkünüz için potansiyel alıcılarımız mevcut. "
            "Satış sürecinizi hızlandırmak için görüşmek ister misiniz?"
        ),
        "template3": (
            "Merhaba, *\"{title}\"* ilanınız dikkatimi çekti. "
            "Benzer özellikteki mülkler için aktif alıcılarımız var. "
            "Satış sürecinizde size nasıl yardımcı olabilirim?"
        )
    },
    "KIRALIK": {
        "template1": (
            "Merhaba, ilanınız *\"{title}\"* kiralık mülklerim arasında dikkatimi çekti. "
            "Kiralama sürecini hızlıca yönetmek ister misiniz?"
        ),
        "template2": (
            "Merhaba, *\"{title}\"* ilanınızı gördüm. "
            "Kiralık mülk arayan müşterilerimiz mevcut. "
            "Kiracı bulma sürecinizde size yardımcı olabilirim."
        ),
        "template3": (
            "Merhaba, *\"{title}\"* ilanınız için potansiyel kiracılarımız var. "
            "Kiralama sürecinizi hızlandırmak için görüşmek ister misiniz?"
        )
    }
}
DEFAULT_TEMPLATE = (
    "Merhaba, ilanınız *\"{title}\"* hakkında bilgi vermek isterim."
)

DELAY_BETWEEN_MESSAGES = 5  # seconds

# --- Test Mode Configuration ---
TEST_MODE = True  # Set to False to disable test mode and use real numbers
TEST_PHONE = "+905372131504"  # Replace with your number in international format

def main(csv_file=None, thread=None, test_mode=False, test_phone="", selected_templates=None, custom_template=None):
    driver = None
    try:
        # Test modu kontrolü
        if test_mode and test_phone:
            logging.info(f"🧪 Test modu aktif: Mesajlar {test_phone} numarasına gidecek")
            test_phone = test_phone.replace("+", "").replace(" ", "")
        else:
            logging.info("⚠️ Test modu kapalı: Gerçek numaralara mesaj gönderilecek")

        # Eğer thread varsa ve driver zaten başlatılmışsa, yeni driver başlatma
        if thread and thread.driver:
            driver = thread.driver
            logging.info("✅ Mevcut ChromeDriver kullanılıyor.")
        else:
            # 1) Start WebDriver
            options = webdriver.ChromeOptions()
            try:
                # Apple Silicon için özel ayarlar
                if sys.platform == 'darwin' and platform.machine() == 'arm64':
                    options.binary_location = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
                    driver = webdriver.Chrome(options=options)
                else:
                    # Diğer platformlar için webdriver-manager kullan
                    driver_path = ChromeDriverManager().install()
                    service = Service(driver_path)
                    driver = webdriver.Chrome(service=service, options=options)
                logging.info("✅ ChromeDriver başarıyla başlatıldı.")
            except Exception as e:
                logging.warning(f"ChromeDriver başlatılamadı: {e}")
                logging.info("PATH içindeki chromedriver kullanılacak, lütfen kurulu olduğundan emin olun.")
                driver = webdriver.Chrome(options=options)

        # 2) Open WhatsApp Web and wait for login
        if not thread or not thread.driver:
            driver.get("https://web.whatsapp.com")
            logging.info("❗ Lütfen WhatsApp Web'e QR kod ile giriş yapın ve hazır olduğunuzda Enter'a basın...")
            
            # If running in thread, wait for manual confirmation
            if thread:
                thread.manual_confirmation_needed.emit()
                while not thread.manual_confirmation:
                    time.sleep(0.1)
                    if thread.should_stop:
                        return
            else:
                input()

        # 3) Load CSV
        csv_path = csv_file if csv_file else DEFAULT_CSV_FILE
        try:
            # Önce dosyanın varlığını kontrol et
            if not os.path.exists(csv_path):
                raise FileNotFoundError(f"CSV dosyası bulunamadı: {csv_path}")

            # CSV dosyasını okumayı dene
            try:
                # Önce dosyanın içeriğini kontrol et
                with open(csv_path, 'r', encoding='utf-8') as f:
                    first_line = f.readline().strip()
                    delimiter = ',' if ',' in first_line else ';' if ';' in first_line else '\t'
                
                # Delimiter'ı kullanarak CSV'yi oku
                df = pd.read_csv(csv_path, encoding='utf-8', sep=delimiter, on_bad_lines='skip')
                
                # Gerekli sütunları kontrol et ve yeniden adlandır
                required_columns = {
                    'Telefon': ['Telefon', 'telefon', 'PHONE', 'phone'],
                    'Ilan Basligi': ['Ilan Basligi', 'İlan Başlığı', 'ILAN BASLIGI', 'ilan_basligi'],
                    'IslemTipi': ['IslemTipi', 'İşlem Tipi', 'ISLEMTIPI', 'islem_tipi']
                }
                
                # Sütun isimlerini standartlaştır
                for standard_name, possible_names in required_columns.items():
                    found = False
                    for col in df.columns:
                        if col in possible_names:
                            df = df.rename(columns={col: standard_name})
                            found = True
                            break
                    if not found:
                        raise ValueError(f"CSV dosyasında gerekli sütun bulunamadı: {standard_name}")

                logging.info(f"✅ CSV dosyası yüklendi: {csv_path}")
                logging.info(f"📊 Toplam {len(df)} kayıt bulundu")

            except UnicodeDecodeError:
                # UTF-8 başarısız olursa UTF-8-sig ile dene
                df = pd.read_csv(csv_path, encoding='utf-8-sig', sep=delimiter, on_bad_lines='skip')
            except Exception as e:
                error_msg = f"CSV dosyası okunurken hata oluştu: {str(e)}"
                logging.error(error_msg)
                raise

        except Exception as e:
            error_msg = f"CSV dosyası okunurken hata oluştu: {str(e)}"
            logging.error(error_msg)
            raise

        # 4) Deduplicate by phone
        df_unique = df.drop_duplicates(subset=["Telefon"]).reset_index(drop=True)
        logging.info(f"📊 Toplam {len(df_unique)} benzersiz telefon numarası bulundu")

        # 5) Send messages
        for idx, row in df_unique.iterrows():
            # Check if should stop
            if thread and thread.should_stop:
                logging.info("WhatsApp bot durduruldu.")
                break

            # Check if paused
            while thread and thread.is_paused:
                time.sleep(0.1)
                if thread.should_stop:
                    logging.info("WhatsApp bot durduruldu.")
                    return

            # Test modunda telefon numarasını override et
            phone = test_phone if test_mode else row["Telefon"].replace("+", "").replace(" ", "")
            title = row.get("Ilan Basligi", "").strip()
            islem = row.get("IslemTipi", "").strip().upper()
            
            # Seçili şablonları kullan
            if selected_templates and islem in selected_templates:
                templates = selected_templates[islem]
                if templates:
                    # Özel şablon varsa ve seçiliyse
                    if "custom" in templates and custom_template:
                        template = custom_template
                    else:
                        # Rastgele bir şablon seç
                        template_key = random.choice(templates)
                        template = MESSAGE_TEMPLATES[islem][template_key]
                else:
                    template = DEFAULT_TEMPLATE
            else:
                template = DEFAULT_TEMPLATE

            message = template.format(title=title)
            encoded_msg = urllib.parse.quote_plus(message)
            url = f"https://web.whatsapp.com/send?phone={phone}&text={encoded_msg}"

            driver.get(url)
            # Wait for message input to be visible (more specific selector)
            input_box = WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'div[contenteditable="true"][data-tab="10"]'))
            )
            # Ensure the input is focused
            input_box.click()
            time.sleep(0.5)
            time.sleep(1)

            # Press ENTER to send message
            input_box.send_keys(Keys.ENTER)
            time.sleep(1)
            logging.info(f"✅ Mesaj gönderildi: {phone}")

            time.sleep(DELAY_BETWEEN_MESSAGES)

        logging.info("✅ Tüm mesajlar işlendi.")

    except Exception as e:
        logging.error(f"Beklenmeyen hata: {str(e)}")
        raise
    finally:
        if driver and not thread:
            try:
                driver.quit()
            except:
                pass

if __name__ == "__main__":
    main()