from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
from mpesa_utils import get_access_token, initiate_stk_push
from routeros_api import RouterOsApiPool
from sqlalchemy import func, text
from datetime import datetime, timedelta
import time
import os
from dotenv import load_dotenv

# -----------------------------
# Load environment variables
# -----------------------------
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

# Database
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    "DATABASE_URI", "sqlite:///your_database.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# MPESA credentials from .env
consumer_key = os.environ.get("MPESA_CONSUMER_KEY")
consumer_secret = os.environ.get("MPESA_CONSUMER_SECRET")
passkey = os.environ.get("MPESA_PASSKEY")
business_short_code = os.environ.get("MPESA_SHORTCODE")
callback_url = os.environ.get("MPESA_CALLBACK_URL")

# -----------------------------
# Models
# -----------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Admin(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False)
    package = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    expiry_date = db.Column(db.DateTime)
    account_name = db.Column(db.String(100))


class Package(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    amount = db.Column(db.Integer, nullable=False)

# -----------------------------
# DB create + seed
# -----------------------------
with app.app_context():
    db.create_all()

    if Package.query.count() == 0:
        default_packages = [
            Package(name="3mbps monthly", amount=1000),
            Package(name="6mbps monthly", amount=1500),
            Package(name="10mbps monthly", amount=2000),
            Package(name="28mbps monthly", amount=2500),
            Package(name="35mbps monthly", amount=3000),
        ]
        db.session.add_all(default_packages)
        db.session.commit()

    if not Admin.query.filter_by(username='admin').first():
        hashed_pwd = generate_password_hash('admin123')
        admin = Admin(username='admin', password=hashed_pwd)
        db.session.add(admin)
        db.session.commit()

# -----------------------------
# MikroTik cache
# -----------------------------
mikrotik_cache = {"timestamp": 0, "pppoe": 0}

# -----------------------------
# Public / Auth routes
# -----------------------------
@app.route('/')
def home():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name'].strip()
        phone = request.form['phone'].strip()
        password = request.form['password']
        confirm = request.form['confirm_password']

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return redirect(url_for('register'))

        hashed_password = generate_password_hash(password)
        new_user = User(name=name, phone=phone, password=hashed_password)

        try:
            db.session.add(new_user)
            db.session.commit()
            flash("Account created. Please log in.", "success")
            return redirect(url_for('login'))
        except Exception:
            flash("Phone number already registered.", "danger")
            return redirect(url_for('register'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        phone = request.form['phone'].strip()
        password = request.form['password']
        user = User.query.filter_by(phone=phone).first()

        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id
            session['user_name'] = user.name
            return redirect(url_for('packages'))
        else:
            flash("Invalid phone number or password.", "danger")
            return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        phone = request.form['phone'].strip()
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']

        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for('forgot_password'))

        user = User.query.filter_by(phone=phone).first()
        if user:
            user.password = generate_password_hash(new_password)
            db.session.commit()
            flash("Password reset. Please log in.", "success")
            return redirect(url_for('login'))
        else:
            flash("Phone number not found.", "danger")
            return redirect(url_for('forgot_password'))

    return render_template('forgot_password.html')

# -----------------------------
# User Packages
# -----------------------------
@app.route('/packages')
def packages():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    packages = Package.query.all()
    active_payment = Payment.query.filter_by(
        phone=User.query.get(session['user_id']).phone,
        status='Completed'
    ).order_by(Payment.timestamp.desc()).first()

    return render_template('packages.html', packages=packages, active_payment=active_payment)

# -----------------------------
# Payment routes
# -----------------------------
@app.route('/payment/<int:package_id>', methods=['GET', 'POST'])
def payment(package_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    package = db.session.get(Package, package_id)
    user = db.session.get(User, session['user_id'])

    if not package or not user:
        flash("Invalid operation.", "danger")
        return redirect(url_for('packages'))

    if request.method == 'POST':
        phone = request.form.get("phone", "").strip()
        if phone.startswith("0"):
            phone = "254" + phone[1:]
        elif phone.startswith("+254"):
            phone = phone[1:]
        elif not phone.startswith("254"):
            flash("Invalid phone number format.", "danger")
            return redirect(url_for('payment', package_id=package_id))

        new_payment = Payment(
            phone=phone,
            amount=package.amount,
            status="Pending",
            package=package.name,
            account_name=user.name
        )
        db.session.add(new_payment)
        db.session.commit()

        response = initiate_stk_push(
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            business_short_code=business_short_code,
            passkey=passkey,
            amount=package.amount,
            phone_number=phone,
            callback_url=callback_url,
            account_reference=user.name[:20],
            transaction_desc=f"{package.name} Subscription"
        )
        print("📤 STK Push Response:", response)
        flash(f"✅ Payment request sent. Complete payment for {user.name}.", "success")

    return render_template('payment.html', package_name=package.name, package_id=package.id)


@app.route('/callback', methods=['POST'])
def callback():
    data = request.get_json()
    print("📩 Callback received:", data)
    try:
        stk = data['Body']['stkCallback']
        result_code = stk['ResultCode']
        if result_code == 0:
            items = {item['Name']: item.get('Value') for item in stk['CallbackMetadata']['Item']}
            phone = str(items.get('PhoneNumber', ''))
            amount = int(items.get('Amount', 0))

            payment = Payment.query.filter_by(phone=phone, amount=amount, status='Pending') \
                        .order_by(Payment.timestamp.desc()).first()
            if payment:
                payment.status = 'Completed'
                payment.expiry_date = datetime.utcnow() + timedelta(days=30)
                db.session.commit()
            else:
                db.session.add(Payment(
                    phone=phone, amount=amount, status='Completed',
                    package=None, account_name=None,
                    timestamp=datetime.utcnow(),
                    expiry_date=datetime.utcnow() + timedelta(days=30)
                ))
                db.session.commit()
    except Exception as e:
        print("❌ Error handling callback:", e)
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

# -----------------------------
# Token helper
# -----------------------------
@app.route('/get_token')
def get_token():
    token = get_access_token(consumer_key, consumer_secret)
    return f"Access Token: {token}"

# -----------------------------
# Logout
# -----------------------------
@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', "info")
    return redirect(url_for('home'))

# -----------------------------
# Admin routes
# -----------------------------
@app.route('/admin-login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        admin = Admin.query.filter_by(username=username).first()
        if admin and check_password_hash(admin.password, password):
            session['admin_logged_in'] = True
            flash("Welcome back, Admin.", "admin-success")
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid admin credentials', 'admin-danger')
    return render_template('admin/login.html')

@app.route('/admin/change-credentials', methods=['GET', 'POST'])
def admin_change_credentials():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    admin = Admin.query.first()  # Assuming single admin user
    if request.method == 'POST':
        current_password = request.form['current_password']
        new_username = request.form['new_username']
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']

        # Validate current password
        if not check_password_hash(admin.password, current_password):
            flash("Current password is incorrect.", "admin-danger")
            return redirect(url_for('admin_change_credentials'))

        # Validate new password match
        if new_password != confirm_password:
            flash("New passwords do not match.", "admin-danger")
            return redirect(url_for('admin_change_credentials'))

        # Update admin credentials
        if new_username:
            admin.username = new_username
        if new_password:
            admin.password = generate_password_hash(new_password)

        db.session.commit()
        flash("Admin credentials updated successfully!", "admin-success")
        return redirect(url_for('admin_dashboard'))

    return render_template('admin/change_credentials.html', admin=admin)


# -----------------------------------------------------------------------------
# Admin Dashboard (PPPoE only)
# -----------------------------------------------------------------------------
@app.route('/admin/dashboard')
def admin_dashboard():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    # Totals
    user_count = User.query.count()
    package_count = Package.query.count()  # not currently shown, but passed if needed
    total_payments = db.session.query(func.sum(Payment.amount)).scalar() or 0

    # Recent items
    recent_users = User.query.order_by(User.id.desc()).limit(5).all()
    recent_payments = Payment.query.filter_by(status='Completed').order_by(Payment.timestamp.desc()).limit(5).all()

    # Dates for reporting
    now = datetime.utcnow()
    six_months_ago = now - timedelta(days=180)

    # Daily payments (last 7)
    daily_payments = (
        db.session.query(
            func.date(Payment.timestamp).label('date'),
            func.sum(Payment.amount).label('total')
        )
        .filter(Payment.status == 'Completed')
        .group_by(func.date(Payment.timestamp))
        .order_by(func.date(Payment.timestamp).desc())
        .limit(7)
        .all()
    )
    daily_labels = [str(d[0]) for d in daily_payments][::-1]
    daily_data = [d[1] for d in daily_payments][::-1]

    # Monthly payments (last 6)
    monthly_payments = (
        db.session.query(
            func.strftime('%Y-%m', Payment.timestamp).label('month'),
            func.sum(Payment.amount).label('total')
        )
        .filter(Payment.status == 'Completed')
        .group_by('month')
        .order_by(text('month desc'))
        .limit(6)
        .all()
    )
    monthly_labels = [m[0] for m in monthly_payments][::-1]
    monthly_data = [m[1] for m in monthly_payments][::-1]

    # Retention (6 months)
    start_users = User.query.filter(User.created_at <= six_months_ago).count()
    new_users = User.query.filter(User.created_at > six_months_ago).count()
    retained_user_ids = (
        db.session.query(Payment.phone)
        .filter(Payment.timestamp >= six_months_ago, Payment.status == 'Completed')
        .distinct()
        .all()
    )
    retained_users = len(retained_user_ids)
    retention_rate = 0
    if start_users > 0:
        retention_rate = round(((retained_users - new_users) / start_users) * 100, 2)

    # MikroTik PPPoE active count (cached 30s)
    pppoe_count = mikrotik_cache.get("pppoe", 0)
    try:
        if time.time() - mikrotik_cache.get("timestamp", 0) > 30:
            api_pool = RouterOsApiPool('10.10.0.1', 'admin', 'TheLion', plaintext_login=True)
            api = api_pool.get_api()
            pppoe_active = api.get_resource('/ppp/active').get()
            pppoe_count = len(pppoe_active)
            mikrotik_cache["pppoe"] = pppoe_count
            mikrotik_cache["timestamp"] = time.time()
            api_pool.disconnect()
    except Exception as e:
        flash(f"⚠️ MikroTik connection failed: {e}", "admin-warning")

    return render_template(
        'admin/dashboard.html',
        user_count=user_count,
        package_count=package_count,
        total_payments=total_payments,
        recent_users=recent_users,
        recent_payments=recent_payments,
        pppoe_count=pppoe_count,
        daily_labels=daily_labels,
        daily_data=daily_data,
        monthly_labels=monthly_labels,
        monthly_data=monthly_data,
        retention_rate=retention_rate
    )


# -----------------------------------------------------------------------------
# Admin logout
# -----------------------------------------------------------------------------
@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    flash("Admin logged out.", "admin-info")
    return redirect(url_for('admin_login'))


# -----------------------------------------------------------------------------
# Admin users
# -----------------------------------------------------------------------------
@app.route('/admin/users')
def admin_users():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    users = User.query.all()
    return render_template('admin/users.html', users=users)


# -----------------------------------------------------------------------------
# Admin packages
# -----------------------------------------------------------------------------
@app.route('/admin/packages')
def admin_packages():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    packages = Package.query.all()
    return render_template('admin/packages.html', packages=packages)


# -----------------------------------------------------------------------------
# Admin payments (Completed only)
# -----------------------------------------------------------------------------
@app.route('/admin/payments')
def admin_payments():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    payments = Payment.query.order_by(Payment.timestamp.desc()).all()
    return render_template('admin/payments.html', payments=payments)

# -----------------------------------------------------------------------------
# Add / Edit / Delete Packages
# -----------------------------------------------------------------------------
@app.route('/admin/packages/add', methods=['GET', 'POST'])
def add_package():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        name = request.form['name'].strip()
        amount = request.form['amount']
        new_package = Package(name=name, amount=amount)
        db.session.add(new_package)
        db.session.commit()
        flash("New package added successfully.", "admin-success")
        return redirect(url_for('admin_packages'))

    return render_template('admin/add_package.html')


@app.route('/admin/packages/edit/<int:package_id>', methods=['GET', 'POST'])
def edit_package(package_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    package = Package.query.get_or_404(package_id)

    if request.method == 'POST':
        package.name = request.form['name'].strip()
        package.amount = request.form['amount']
        db.session.commit()
        flash("Package updated successfully.", "admin-success")
        return redirect(url_for('admin_packages'))

    return render_template('admin/edit_package.html', package=package)


@app.route('/admin/packages/delete/<int:package_id>', methods=['POST'])
def delete_package(package_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    package = Package.query.get_or_404(package_id)
    db.session.delete(package)
    db.session.commit()
    flash("Package deleted successfully.", "admin-success")
    return redirect(url_for('admin_packages'))


# -----------------------------------------------------------------------------
# Admin PPPoE Usage (basic listing; extend for usage stats later)
# -----------------------------------------------------------------------------
@app.route('/admin/usage', methods=['GET'])
def admin_usage():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    users = []
    try:
        api_pool = RouterOsApiPool('10.10.0.1', 'admin', 'TheLion', plaintext_login=True)
        api = api_pool.get_api()
        ppp_active = api.get_resource('/ppp/active')
        active_users = ppp_active.get()

        # If you want usage stats, extend these fields:
        for u in active_users:
            users.append({
                'name': u.get('name'),
                'caller_id': u.get('caller-id'),
                'uptime': u.get('uptime'),
                'tx_mbytes': round(int(u.get('tx-byte', 0)) / 1048576, 2),
                'rx_mbytes': round(int(u.get('rx-byte', 0)) / 1048576, 2),
            })

        api_pool.disconnect()
    except Exception as e:
        flash("⚠️ Could not connect to MikroTik: " + str(e), "admin-warning")

    return render_template("admin/usage.html", users=users)


# -----------------------------------------------------------------------------
# Admin package performance (placeholder)
# -----------------------------------------------------------------------------
@app.route('/admin/package-performance')
def package_performance():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    packages = Package.query.all()
    performance_data = []
    for pkg in packages:
        performance_data.append({
            'name': pkg.name,
            'amount': pkg.amount,
            'active_users': 0  # TODO: derive from active PPPoE + mapping
        })

    return render_template('admin/package_performance.html', data=performance_data)


# -----------------------------------------------------------------------------
# Admin PPPoE disconnect
# -----------------------------------------------------------------------------
@app.route('/admin/pppoe/disconnect/<string:name>', methods=['POST'])
def disconnect_pppoe_user(name):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    try:
        api_pool = RouterOsApiPool('10.10.0.1', 'admin', 'TheLion', plaintext_login=True)
        api = api_pool.get_api()
        ppp_active = api.get_resource('/ppp/active')

        users = ppp_active.get(name=name)
        for user in users:
            ppp_active.remove(id=user['.id'])

        flash(f"✅ Disconnected user: {name}", "admin-success")
        api_pool.disconnect()
    except Exception as e:
        flash("⚠️ Failed to disconnect user: " + str(e), "admin-danger")

    return redirect(url_for('admin_usage'))


# -----------------------------------------------------------------------------
# Admin Payment Edit
# -----------------------------------------------------------------------------
@app.route('/admin/payments/edit/<int:payment_id>', methods=['GET', 'POST'])
def edit_payment(payment_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    payment = Payment.query.get_or_404(payment_id)

    if request.method == 'POST':
        # Update account name
        new_name = request.form.get('account_name')
        if new_name:
            payment.account_name = new_name.strip()

        # Update expiry date
        new_expiry = request.form.get('expiry_date')
        if new_expiry:
            payment.expiry_date = datetime.strptime(new_expiry, '%Y-%m-%d')

        # Update status
        new_status = request.form.get('status')
        if new_status:
            payment.status = new_status

        db.session.commit()
        flash("Payment details updated successfully!", "admin-success")
        return redirect(url_for('admin_payments'))

    return render_template('admin/edit_payment.html', payment=payment)

# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    # ensure instance dir exists so sqlite path above is valid
    os.makedirs(app.instance_path, exist_ok=True)
    app.run(debug=True, host='0.0.0.0')


