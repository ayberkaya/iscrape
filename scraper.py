import logging
import csv
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
import re
import os
import sys
import platform
import time
import traceback
from datetime import datetime

# --- Helpers ---
# TODO: Future speed-up by parallelizing detail fetch

def get_total_pages(driver, base_url):
    driver.get(base_url)
    WebDriverWait(driver, 10).until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, 'a.page-link[data-page]'))
    )
    elems = driver.find_elements(By.CSS_SELECTOR, 'a.page-link[data-page]')
    nums = [int(e.get_attribute('data-page')) for e in elems if e.get_attribute('data-page') and e.get_attribute('data-page').isdigit()]
    return max(nums) if nums else 1


def get_listing_hrefs(driver, base_url, page):
    url = f"{base_url}&page={page}"
    driver.get(url)
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/app/portfoy/detay/"]'))
    )
    links = [e.get_attribute('href') for e in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/app/portfoy/detay/"]')]
    # unique
    seen = set()
    unique = []
    for l in links:
        if l not in seen:
            seen.add(l)
            unique.append(l)
    return unique


def parse_detail(driver, href):
    try:
        # Navigate and extract fields
        driver.get(href)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'p.description'))
        )
        rec = {}
        rec['Ilan Basligi'] = safe_text(driver, 'p.description')
        rec['IslemTipi']   = safe_text(driver, '.type-container span:nth-child(1)')
        rec['Cinsi']       = safe_text(driver, '.type-container .type')
        rec['Turu']        = safe_text(driver, 'div.col-md-7.col-6.text-right:not(.ad-owner)')
        rec['Bolge']       = safe_text(driver, '.pr-features-right').replace('\n',' ')
        rec['IlanSahibi']  = safe_text(driver, 'div.ad-owner')
        rec['Fiyat']       = safe_text(driver, 'div.price-container')
        ilan_tarihi_text = safe_text(driver, 'div.col-md-7.col-8.text-right')
        rec['IlanTarihi']  = ilan_tarihi_text.split()[-1] if ilan_tarihi_text else ''
        # Listing source (Ilan Kaynağı)
        try:
            source_el = driver.find_element(
                By.XPATH,
                '//div[@class="col-md-5 col-6" and normalize-space(text())="İlan Kaynağı"]/following-sibling::div[@class="col-md-7 col-6 text-right"]'
            )
            rec['Ilan Kaynağı'] = source_el.text.strip()
        except NoSuchElementException:
            rec['Ilan Kaynağı'] = ''
        # Telefon
        try:
            rec['Telefon'] = driver.find_element(By.CSS_SELECTOR, 'a[href^="tel:"]').text.strip()
        except:
            rec['Telefon']=''
        if not rec['Telefon']:
            m = re.search(r'0\s?\d{3}\s?\d{3}\s?\d{2}\s?\d{2}', driver.page_source)
            rec['Telefon'] = m.group(0).replace(' ','') if m else ''
        
        if not rec['Telefon']:
            logging.warning(f"⚠️ Telefon bulunamadı: {href}")
            return None
            
        if not rec['Ilan Basligi']:
            logging.warning(f"⚠️ İlan başlığı bulunamadı: {href}")
            return None
        
        # İlan linkini ekle
        rec['Ilan Linki'] = href
            
        return rec
    except Exception as e:
        logging.error(f"❌ İlan detayı çekilirken hata: {href} - {str(e)}")
        return None


def safe_text(driver, selector):
    try:
        return driver.find_element(By.CSS_SELECTOR, selector).text.strip()
    except:
        return ''


def main(listing_type="Yayındaki İlanlar", sort_by="Varsayılan sıralama (tarih ↓)", save_path=None, thread=None, custom_filename="revy_ilanlar"):
    try:
        # URL parametrelerini ayarla
        base_url = "https://www.revy.com.tr/app/portfoy/ilanlar?export=0&fsbo=true"
        if listing_type == "Yayından Kaldırılan İlanlar":
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
        sort_param = sort_params.get(sort_by, "date_desc")
        url = f"{base_url}&sort={sort_param}"
        
        # FSBO sayfasını aç
        thread.driver.get(url)
        logging.info("✅ FSBO sayfası açıldı")
        
        # Sayfanın yüklenmesini bekle
        time.sleep(5)
        
        # Toplam ilan sayısını al
        try:
            total_ads_element = thread.driver.find_element("id", "totalAdvertisement")
            total_ads = int(total_ads_element.text.replace(".", ""))
            if thread:
                thread.total_ads_updated.emit(total_ads)
            logging.info(f"Toplam ilan sayısı: {total_ads}")
        except Exception as e:
            logging.warning(f"Toplam ilan sayısı alınamadı: {e}")
            total_ads = 0
        
        # Toplam sayfa sayısını al
        try:
            page_links = thread.driver.find_elements("css selector", "a.page-link[data-page]")
            total_pages = max([int(link.get_attribute("data-page")) for link in page_links])
            logging.info(f"Toplam sayfa sayısı: {total_pages}")
        except Exception as e:
            logging.warning(f"Toplam sayfa sayısı alınamadı: {e}")
            total_pages = 1
        
        # CSV dosyasını oluştur
        filename = f"{custom_filename}.csv"
        if save_path:
            filename = os.path.join(save_path, filename)
            logging.info(f"CSV dosyası oluşturuldu: {filename}")
        
        # DataFrame'i oluştur
        df = pd.DataFrame(columns=[
            'Ilan Basligi', 'IslemTipi', 'Cinsi', 'Turu', 'Bolge', 
            'IlanSahibi', 'Telefon', 'Fiyat', 'IlanTarihi', 'Ilan Kaynağı', 'Ilan Linki'
        ])
        
        # İlk sayfayı kaydet
        df.to_csv(filename, index=False, encoding='utf-8-sig')
        
        # İşlenmiş linkleri takip et
        processed_links = set()
        
        # Her sayfayı işle
        for page in range(1, total_pages + 1):
            if thread and thread.should_stop:
                break
                
            if page > 1:
                # Sayfa URL'ini oluştur
                page_url = f"{url}&page={page}"
                thread.driver.get(page_url)
                time.sleep(5)  # Sayfa yüklenmesini bekle
            
            # İlan linklerini topla
            listing_links = get_listing_hrefs(thread.driver, base_url, page)
            
            # Her ilanı işle
            for href in listing_links:
                if thread and thread.should_stop:
                    break
                    
                if href in processed_links:
                    continue
                    
                processed_links.add(href)
                
                try:
                    # İlan detaylarını al
                    ad = parse_detail(thread.driver, href)
                    if ad:
                        # Yeni veriyi CSV'ye ekle
                        new_df = pd.DataFrame([ad])
                        new_df.to_csv(filename, mode='a', header=False, index=False, encoding='utf-8-sig')
                        
                        # İlerleme bilgisini güncelle
                        if thread:
                            thread.progress_updated.emit(len(processed_links))
                        
                        logging.info(f"✅ Veri Çekildi: {ad['Ilan Basligi']}")
                except Exception as e:
                    logging.error(f"İlan detayları alınamadı: {href} - Hata: {e}")
                    continue
            
            # Sayfa ilerleme bilgisini güncelle
            if thread:
                thread.page_progress_updated.emit(page)
        
        logging.info(f"Toplam {len(processed_links)} ilan başarıyla kaydedildi.")
        return filename
        
    except Exception as e:
        logging.error(f"Scraper hatası: {str(e)}")
        raise


if __name__ == "__main__":
    main()