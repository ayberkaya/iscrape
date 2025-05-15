from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_migrate import Migrate
import os
from dotenv import load_dotenv
import stripe
from datetime import datetime, timedelta
import secrets
import hashlib
import requests
import hmac
import json
from celery import Celery
import tempfile
import redis
import uuid
from celery.result import AsyncResult
from werkzeug.utils import secure_filename
import threading
import time
import pandas as pd
import random
import urllib.parse
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager
import platform
import sys

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///app.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_PUBLIC_KEY = os.getenv('STRIPE_PUBLIC_KEY')

# Initialize Celery
celery = Celery(
    app.name,
    broker=os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0'),
    backend=os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
)
celery.conf.update(app.config)

# Shopier Configuration
SHOPIER_API_KEY = os.getenv('SHOPIER_API_KEY')
SHOPIER_API_SECRET = os.getenv('SHOPIER_API_SECRET')
SHOPIER_API_URL = os.getenv('SHOPIER_API_URL', 'https://www.shopier.com/api')

# Initialize database
db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Initialize login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Redis setup for progress/logs
redis_client = redis.StrictRedis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'))

# Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    subscription_tier = db.Column(db.String(20), default='free')
    subscription_status = db.Column(db.String(20), default='active')
    subscription_end = db.Column(db.DateTime)
    stripe_customer_id = db.Column(db.String(100))
    shopier_customer_id = db.Column(db.String(100))
    api_key = db.Column(db.String(100), unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ScrapingJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey('template.id'))
    status = db.Column(db.String(20), default='pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    result = db.Column(db.Text)

class Template(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    content = db.Column(db.Text, nullable=False)
    is_premium = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Helper functions
def generate_api_key():
    return secrets.token_urlsafe(32)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def get_subscription_limits(tier):
    limits = {
        'free': {
            'daily_jobs': 10,
            'templates': ['basic'],
            'export_formats': ['csv']
        },
        'pro': {
            'daily_jobs': 100,
            'templates': ['basic', 'premium'],
            'export_formats': ['csv', 'json', 'excel']
        },
        'enterprise': {
            'daily_jobs': float('inf'),
            'templates': ['basic', 'premium', 'custom'],
            'export_formats': ['csv', 'json', 'excel']
        }
    }
    return limits.get(tier, limits['free'])

# Shopier Helper Functions
def generate_shopier_signature(data):
    """Generate Shopier signature for API requests"""
    message = json.dumps(data, sort_keys=True)
    signature = hmac.new(
        SHOPIER_API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature

def create_shopier_payment(user, plan):
    """Create a payment request in Shopier"""
    price = {
        'pro': 29.99,
        'enterprise': 99.99
    }.get(plan, 0)
    
    if price == 0:
        return None
    
    data = {
        'api_key': SHOPIER_API_KEY,
        'website_index': 1,
        'platform_order_id': f"order_{user.id}_{int(datetime.utcnow().timestamp())}",
        'product_name': f"iScrape {plan.title()} Plan",
        'product_type': 'Subscription',
        'buyer_name': user.email,
        'buyer_email': user.email,
        'amount': price,
        'currency': 'TRY',
        'callback_url': f"{request.host_url}api/shopier/callback",
        'platform': 'iScrape',
        'is_in_frame': 0,
        'current_language': 'tr-TR'
    }
    
    data['signature'] = generate_shopier_signature(data)
    
    try:
        response = requests.post(f"{SHOPIER_API_URL}/payment", json=data)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Shopier API error: {str(e)}")
        return None

# Celery Tasks
@celery.task
def process_scraping_job(job_id):
    """Process a scraping job asynchronously"""
    job = ScrapingJob.query.get(job_id)
    if not job:
        return
    
    try:
        # Initialize Chrome driver
        options = webdriver.ChromeOptions()
        if sys.platform == 'darwin' and platform.machine() == 'arm64':
            options.binary_location = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
            driver = webdriver.Chrome(options=options)
        else:
            driver_path = ChromeDriverManager().install()
            service = Service(driver_path)
            driver = webdriver.Chrome(service=service, options=options)

        # Get template content
        template = Template.query.get(job.template_id)
        if not template:
            raise ValueError("Template not found")

        # Parse template content
        template_config = json.loads(template.content)

        # Start scraping process
        base_url = job.url
        driver.get(base_url)
        time.sleep(5)  # Wait for page load

        # Get total pages
        try:
            page_links = driver.find_elements(By.CSS_SELECTOR, "a.page-link[data-page]")
            total_pages = max([int(link.get_attribute("data-page")) for link in page_links])
        except Exception as e:
            total_pages = 1

        # Initialize results
        results = []
        processed_links = set()

        # Process each page
        for page in range(1, total_pages + 1):
            if page > 1:
                page_url = f"{base_url}&page={page}"
                driver.get(page_url)
                time.sleep(5)

            # Get listing links
            links = [e.get_attribute('href') for e in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/app/portfoy/detay/"]')]
            unique_links = [l for l in links if l not in processed_links]

            # Process each listing
            for href in unique_links:
                processed_links.add(href)
                try:
                    driver.get(href)
                    WebDriverWait(driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, 'p.description'))
                    )

                    # Extract data based on template configuration
                    data = {}
                    for field, selector in template_config.items():
                        try:
                            data[field] = driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                        except:
                            data[field] = ''

                    # Add listing URL
                    data['Ilan Linki'] = href

                    results.append(data)
                except Exception as e:
                    continue

        # Save results
        if results:
            df = pd.DataFrame(results)
            output_path = f"results/job_{job_id}.csv"
            os.makedirs("results", exist_ok=True)
            df.to_csv(output_path, index=False, encoding='utf-8-sig')
            
            job.status = 'completed'
            job.completed_at = datetime.utcnow()
            job.result = output_path
        else:
            job.status = 'failed'
            job.result = 'No data found'

        db.session.commit()

    except Exception as e:
        job.status = 'failed'
        job.result = str(e)
        db.session.commit()
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

# WhatsApp Bot Celery Task
@celery.task(bind=True)
def whatsapp_bot_task(self, user_id, csv_path, test_mode, test_phone, selected_templates, custom_template):
    import time
    import pandas as pd
    import random
    import urllib.parse
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.keys import Keys
    from webdriver_manager.chrome import ChromeDriverManager
    import platform
    import sys
    import os

    MESSAGE_TEMPLATES = {
        "SATILIK": {
            "template1": "Merhaba, ilanınız *\"{title}\"* satışa sunduğunuz bu mülk için alıcı portföyümüze eklenebilir. Süreci hızlandırmak isterseniz yardımcı olabilirim.",
            "template2": "Merhaba, *\"{title}\"* ilanınızı inceledim. Mülkünüz için potansiyel alıcılarımız mevcut. Satış sürecinizi hızlandırmak için görüşmek ister misiniz?",
            "template3": "Merhaba, *\"{title}\"* ilanınız dikkatimi çekti. Benzer özellikteki mülkler için aktif alıcılarımız var. Satış sürecinizde size nasıl yardımcı olabilirim?"
        },
        "KIRALIK": {
            "template1": "Merhaba, ilanınız *\"{title}\"* kiralık mülklerim arasında dikkatimi çekti. Kiralama sürecini hızlıca yönetmek ister misiniz?",
            "template2": "Merhaba, *\"{title}\"* ilanınızı gördüm. Kiralık mülk arayan müşterilerimiz mevcut. Kiracı bulma sürecinizde size yardımcı olabilirim.",
            "template3": "Merhaba, *\"{title}\"* ilanınız için potansiyel kiracılarımız var. Kiralama sürecinizi hızlandırmak için görüşmek ister misiniz?"
        }
    }
    DEFAULT_TEMPLATE = "Merhaba, ilanınız *\"{title}\"* hakkında bilgi vermek isterim."
    DELAY_BETWEEN_MESSAGES = 5

    task_id = self.request.id
    progress_key = f"wa_progress:{task_id}"
    log_key = f"wa_log:{task_id}"
    state_key = f"wa_state:{task_id}"
    result_csv = f"whatsapp_results_{task_id}.csv"
    redis_client.set(progress_key, 0)
    redis_client.delete(log_key)
    redis_client.set(state_key, "waiting_login")

    def log(msg):
        redis_client.rpush(log_key, msg)

    driver = None
    try:
        log("Başlatılıyor...")
        if test_mode and test_phone:
            log(f"🧪 Test modu aktif: Mesajlar {test_phone} numarasına gidecek")
            test_phone = test_phone.replace("+", "").replace(" ", "")
        else:
            log("⚠️ Test modu kapalı: Gerçek numaralara mesaj gönderilecek")

        # Start WebDriver
        options = webdriver.ChromeOptions()
        try:
            if sys.platform == 'darwin' and platform.machine() == 'arm64':
                options.binary_location = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
                driver = webdriver.Chrome(options=options)
            else:
                driver_path = ChromeDriverManager().install()
                service = Service(driver_path)
                driver = webdriver.Chrome(service=service, options=options)
            log("✅ ChromeDriver başarıyla başlatıldı.")
        except Exception as e:
            log(f"ChromeDriver başlatılamadı: {e}")
            driver = webdriver.Chrome(options=options)

        # Open WhatsApp Web and wait for login
        driver.get("https://web.whatsapp.com")
        log("❗ Lütfen WhatsApp Web'e QR kod ile giriş yapın ve web arayüzünden 'Devam Et' butonuna tıklayın...")
        redis_client.set(state_key, "waiting_login")
        # Wait for manual confirmation from frontend
        while redis_client.get(state_key).decode() == "waiting_login":
            time.sleep(1)
        log("✅ Giriş onaylandı, mesaj gönderimine başlanıyor...")
        redis_client.set(state_key, "running")

        # Load CSV
        if not os.path.exists(csv_path):
            log(f"CSV dosyası bulunamadı: {csv_path}")
            redis_client.set(state_key, "failed")
            return
        with open(csv_path, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            delimiter = ',' if ',' in first_line else ';' if ';' in first_line else '\t'
        df = pd.read_csv(csv_path, encoding='utf-8', sep=delimiter, on_bad_lines='skip')
        # Standardize columns
        required_columns = {
            'Telefon': ['Telefon', 'telefon', 'PHONE', 'phone'],
            'Ilan Basligi': ['Ilan Basligi', 'İlan Başlığı', 'ILAN BASLIGI', 'ilan_basligi'],
            'IslemTipi': ['IslemTipi', 'İşlem Tipi', 'ISLEMTIPI', 'islem_tipi']
        }
        for standard_name, possible_names in required_columns.items():
            found = False
            for col in df.columns:
                if col in possible_names:
                    df = df.rename(columns={col: standard_name})
                    found = True
                    break
            if not found:
                log(f"CSV dosyasında gerekli sütun bulunamadı: {standard_name}")
                redis_client.set(state_key, "failed")
                return
        df_unique = df.drop_duplicates(subset=["Telefon"]).reset_index(drop=True)
        log(f"📊 Toplam {len(df_unique)} benzersiz telefon numarası bulundu")

        # Send messages
        sent_rows = []
        for idx, row in df_unique.iterrows():
            if redis_client.get(state_key).decode() == "stopped":
                log("Kullanıcı tarafından durduruldu.")
                break
            phone = test_phone if test_mode else row["Telefon"].replace("+", "").replace(" ", "")
            title = row.get("Ilan Basligi", "").strip()
            islem = row.get("IslemTipi", "").strip().upper()
            if selected_templates and islem in selected_templates:
                templates = selected_templates[islem]
                if templates:
                    if "custom" in templates and custom_template:
                        template = custom_template
                    else:
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
            try:
                input_box = WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'div[contenteditable="true"][data-tab="10"]'))
                )
                input_box.click()
                time.sleep(0.5)
                input_box.send_keys(Keys.ENTER)
                time.sleep(1)
                log(f"✅ Mesaj gönderildi: {phone}")
                sent_rows.append(row)
            except Exception as e:
                log(f"❌ Mesaj gönderilemedi: {phone} - {e}")
            redis_client.set(progress_key, int((idx+1)/len(df_unique)*100))
            time.sleep(DELAY_BETWEEN_MESSAGES)
        # Save results
        pd.DataFrame(sent_rows).to_csv(result_csv, index=False)
        redis_client.set(state_key, "completed")
        log(f"✅ Tüm mesajlar işlendi. Sonuçlar indirilebilir.")
        redis_client.set(f"wa_result:{task_id}", result_csv)
    except Exception as e:
        log(f"Beklenmeyen hata: {str(e)}")
        redis_client.set(state_key, "failed")
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

# Routes
@app.route('/')
def index():
    """Ana sayfa route'u"""
    try:
        return render_template('index.html')
    except Exception as e:
        app.logger.error(f"Index route error: {str(e)}")
        return str(e), 500

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        
        if user and user.password == hash_password(password):
            login_user(user)
            return redirect(url_for('dashboard'))
        
        flash('Invalid email or password')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        plan = request.form.get('plan')
        
        if password != confirm_password:
            flash('Passwords do not match')
            return redirect(url_for('register'))
        
        if User.query.filter_by(email=email).first():
            flash('Email already registered')
            return redirect(url_for('register'))
        
        # Create Stripe customer if paid plan
        stripe_customer_id = None
        if plan != 'free':
            stripe_token = request.form.get('stripeToken')
            if not stripe_token:
                flash('Payment information required for paid plans')
                return redirect(url_for('register'))
            
            try:
                customer = stripe.Customer.create(
                    email=email,
                    source=stripe_token
                )
                stripe_customer_id = customer.id
                
                # Create subscription
                price_id = {
                    'pro': 'price_pro_monthly',
                    'enterprise': 'price_enterprise_monthly'
                }.get(plan)
                
                if price_id:
                    stripe.Subscription.create(
                        customer=customer.id,
                        items=[{'price': price_id}]
                    )
            except stripe.error.StripeError as e:
                flash(f'Payment error: {str(e)}')
                return redirect(url_for('register'))
        
        # Create user
        user = User(
            email=email,
            password=hash_password(password),
            subscription_tier=plan,
            stripe_customer_id=stripe_customer_id,
            api_key=generate_api_key()
        )
        
        if plan != 'free':
            user.subscription_end = datetime.utcnow() + timedelta(days=30)
        
        db.session.add(user)
        db.session.commit()
        
        login_user(user)
        return redirect(url_for('dashboard'))
    
    return render_template('register.html', stripe_public_key=STRIPE_PUBLIC_KEY)

@app.route('/dashboard')
@login_required
def dashboard():
    # Get user's daily job count
    today = datetime.utcnow().date()
    daily_jobs = ScrapingJob.query.filter(
        ScrapingJob.user_id == current_user.id,
        ScrapingJob.created_at >= today
    ).count()
    
    # Get total jobs
    total_jobs = ScrapingJob.query.filter_by(user_id=current_user.id).count()
    
    # Calculate success rate
    completed_jobs = ScrapingJob.query.filter_by(
        user_id=current_user.id,
        status='completed'
    ).count()
    success_rate = (completed_jobs / total_jobs * 100) if total_jobs > 0 else 0
    
    # Get recent jobs
    recent_jobs = ScrapingJob.query.filter_by(user_id=current_user.id).order_by(
        ScrapingJob.created_at.desc()
    ).limit(5).all()
    
    # Get available templates
    templates = Template.query.filter(
        (Template.is_premium == False) |  # Free templates
        (current_user.subscription_tier != 'free')  # Premium templates for paid users
    ).all()
    
    # Get subscription limits
    limits = get_subscription_limits(current_user.subscription_tier)
    
    return render_template('dashboard.html',
        daily_jobs=daily_jobs,
        daily_limit=limits['daily_jobs'],
        total_jobs=total_jobs,
        success_rate=round(success_rate, 1),
        recent_jobs=recent_jobs,
        templates=templates
    )

@app.route('/api/scrape', methods=['POST'])
@login_required
def scrape():
    # Check user's subscription and limits
    limits = get_subscription_limits(current_user.subscription_tier)
    
    # Check daily limit
    today = datetime.utcnow().date()
    daily_jobs = ScrapingJob.query.filter(
        ScrapingJob.user_id == current_user.id,
        ScrapingJob.created_at >= today
    ).count()
    
    if daily_jobs >= limits['daily_jobs']:
        return jsonify({'error': 'Daily limit reached'}), 403
    
    # Get request data
    url = request.form.get('url')
    template_id = request.form.get('template')
    
    if not url or not template_id:
        return jsonify({'error': 'URL and template are required'}), 400
    
    # Validate template access
    template = Template.query.get(template_id)
    if not template:
        return jsonify({'error': 'Invalid template'}), 400
    
    if template.is_premium and current_user.subscription_tier == 'free':
        return jsonify({'error': 'Premium template not available in free tier'}), 403
    
    # Create new scraping job
    job = ScrapingJob(
        user_id=current_user.id,
        url=url,
        template_id=template_id
    )
    db.session.add(job)
    db.session.commit()
    
    # Start scraping process asynchronously
    process_scraping_job.delay(job.id)
    
    return jsonify({
        'job_id': job.id,
        'status': 'started',
        'message': 'Scraping job started successfully'
    })

@app.route('/api/job/<int:job_id>')
@login_required
def get_job_status(job_id):
    job = ScrapingJob.query.get_or_404(job_id)
    if job.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    return jsonify({
        'status': job.status,
        'created_at': job.created_at.isoformat(),
        'completed_at': job.completed_at.isoformat() if job.completed_at else None,
        'result': job.result
    })

@app.route('/api/job/<int:job_id>/download')
@login_required
def download_job_results(job_id):
    job = ScrapingJob.query.get_or_404(job_id)
    if job.user_id != current_user.id:
        return jsonify({'error': 'Unauthorized'}), 403
    
    if job.status != 'completed' or not job.result:
        return jsonify({'error': 'No results available'}), 404
    
    return send_file(job.result, as_attachment=True, download_name=f'job_{job_id}_results.csv')

@app.route('/dashboard/upgrade')
@login_required
def upgrade():
    return render_template('upgrade.html', stripe_public_key=STRIPE_PUBLIC_KEY)

@app.route('/api/upgrade', methods=['POST'])
@login_required
def process_upgrade():
    """Handle subscription upgrade"""
    new_plan = request.form.get('plan')
    if not new_plan or new_plan not in ['pro', 'enterprise']:
        return jsonify({'error': 'Invalid plan'}), 400
    
    # Create Shopier payment
    payment = create_shopier_payment(current_user, new_plan)
    if not payment:
        return jsonify({'error': 'Failed to create payment'}), 400
    
    return jsonify({
        'success': True,
        'payment_url': payment.get('payment_url')
    })

@app.route('/api/shopier/callback', methods=['POST'])
def shopier_callback():
    """Handle Shopier payment callback"""
    data = request.form.to_dict()
    signature = data.pop('signature', None)
    
    if not signature or signature != generate_shopier_signature(data):
        return jsonify({'error': 'Invalid signature'}), 400
    
    if data.get('status') != 'success':
        return jsonify({'error': 'Payment failed'}), 400
    
    # Extract order information
    order_id = data.get('platform_order_id')
    user_id = int(order_id.split('_')[1])
    plan = order_id.split('_')[2]
    
    # Update user subscription
    user = User.query.get(user_id)
    if user:
        user.subscription_tier = plan
        user.subscription_status = 'active'
        user.subscription_end = datetime.utcnow() + timedelta(days=30)
        db.session.commit()
    
    return jsonify({'success': True})

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard/templates', methods=['GET', 'POST'])
@login_required
def templates_page():
    if request.method == 'POST':
        name = request.form.get('name')
        description = request.form.get('description')
        content = request.form.get('content')
        is_premium = bool(request.form.get('is_premium'))
        if name and content:
            t = Template(name=name, description=description, content=content, is_premium=is_premium)
            db.session.add(t)
            db.session.commit()
            flash('Template added successfully!')
        return redirect(url_for('templates_page'))
    templates = Template.query.all()
    return render_template('templates.html', templates=templates)

@app.before_first_request
def ensure_sample_template():
    if Template.query.count() == 0:
        # Create default scraping template
        default_template = {
            'Ilan Basligi': 'p.description',
            'IslemTipi': '.type-container span:nth-child(1)',
            'Cinsi': '.type-container .type',
            'Turu': 'div.col-md-7.col-6.text-right:not(.ad-owner)',
            'Bolge': '.pr-features-right',
            'IlanSahibi': 'div.ad-owner',
            'Fiyat': 'div.price-container',
            'IlanTarihi': 'div.col-md-7.col-8.text-right',
            'Telefon': 'a[href^="tel:"]'
        }
        
        t = Template(
            name="Default Scraping Template",
            description="Default template for scraping property listings",
            content=json.dumps(default_template),
            is_premium=False
        )
        db.session.add(t)
        db.session.commit()

@app.route('/dashboard/revy', methods=['GET'])
@login_required
def revy_dashboard():
    return render_template('revy_dashboard.html')

@app.route('/api/scrape-revy', methods=['POST'])
@login_required
def scrape_revy():
    username = request.form.get('username')
    password = request.form.get('password')
    if not username or not password:
        return jsonify({'error': 'Kullanıcı adı ve şifre zorunlu!'}), 400
    
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager
    import pandas as pd
    import time
    import os

    # Geçici dosya oluştur
    temp = tempfile.NamedTemporaryFile(delete=False, suffix='.csv')
    csv_path = temp.name
    temp.close()

    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    driver = None
    try:
        driver_path = ChromeDriverManager().install()
        service = Service(driver_path)
        driver = webdriver.Chrome(service=service, options=options)
        driver.get('https://www.revy.com.tr/login')
        # Giriş formunu doldur
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.NAME, 'phone')))
        driver.find_element(By.NAME, 'phone').send_keys(username)
        driver.find_element(By.NAME, 'password').send_keys(password)
        driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]').click()
        # Başarıyla giriş yapıldığını kontrol et
        WebDriverWait(driver, 20).until(EC.url_contains('/app/portfoy/ilanlar'))
        # İlanlar sayfasına git
        driver.get('https://www.revy.com.tr/app/portfoy/ilanlar?export=0&fsbo=true&area=my&advertisement_status=active')
        time.sleep(5)
        # İlan linklerini topla
        links = [e.get_attribute('href') for e in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/app/portfoy/detay/"]')]
        # Her ilanı işle
        data = []
        for href in links:
            driver.get(href)
            time.sleep(2)
            try:
                title = driver.find_element(By.CSS_SELECTOR, 'p.description').text
            except:
                title = ''
            try:
                price = driver.find_element(By.CSS_SELECTOR, 'div.price-container').text
            except:
                price = ''
            try:
                phone = driver.find_element(By.CSS_SELECTOR, 'a[href^="tel:"]').text
            except:
                phone = ''
            data.append({'Başlık': title, 'Fiyat': price, 'Telefon': phone, 'Link': href})
        # CSV'ye kaydet
        df = pd.DataFrame(data)
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        return send_file(csv_path, as_attachment=True, download_name='revy_ilanlar.csv')
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if driver:
            driver.quit()

# API: Start WhatsApp Bot
@app.route('/api/whatsapp-bot', methods=['POST'])
@login_required
def start_whatsapp_bot():
    # Handle file upload
    if 'csv_file' not in request.files:
        return jsonify({'error': 'CSV file required'}), 400
    file = request.files['csv_file']
    filename = secure_filename(file.filename)
    csv_path = os.path.join('uploads', f"{uuid.uuid4()}_{filename}")
    os.makedirs('uploads', exist_ok=True)
    file.save(csv_path)
    # Parse other params
    test_mode = request.form.get('test_mode', 'false') == 'true'
    test_phone = request.form.get('test_phone', '')
    selected_templates = request.form.get('selected_templates')
    custom_template = request.form.get('custom_template')
    # selected_templates should be a JSON string
    import json
    if selected_templates:
        selected_templates = json.loads(selected_templates)
    # Start task
    task = whatsapp_bot_task.apply_async(args=[current_user.id, csv_path, test_mode, test_phone, selected_templates, custom_template])
    return jsonify({'task_id': task.id})

# API: Poll WhatsApp Bot Progress
@app.route('/api/whatsapp-bot/progress/<task_id>')
@login_required
def whatsapp_bot_progress(task_id):
    progress = int(redis_client.get(f"wa_progress:{task_id}") or 0)
    logs = redis_client.lrange(f"wa_log:{task_id}", 0, -1)
    logs = [l.decode() for l in logs]
    state = redis_client.get(f"wa_state:{task_id}")
    state = state.decode() if state else 'unknown'
    return jsonify({'progress': progress, 'logs': logs, 'state': state})

# API: Continue after login
@app.route('/api/whatsapp-bot/continue/<task_id>', methods=['POST'])
@login_required
def whatsapp_bot_continue(task_id):
    redis_client.set(f"wa_state:{task_id}", "continue")
    return jsonify({'status': 'ok'})

# API: Download result CSV
@app.route('/api/whatsapp-bot/result/<task_id>')
@login_required
def whatsapp_bot_result(task_id):
    result_csv = redis_client.get(f"wa_result:{task_id}")
    if not result_csv:
        return jsonify({'error': 'No result'}), 404
    result_csv = result_csv.decode()
    return send_file(result_csv, as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True) 