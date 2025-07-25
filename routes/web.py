# routes/web.py
from flask import Blueprint, render_template

web_bp = Blueprint('web', __name__)

@web_bp.route('/')
def index():
    """主页面"""
    return render_template('index.html')

@web_bp.route('/accounts/<int:account_id>')
def account_details(account_id):
    """服务账号详情页面"""
    return render_template('account_details.html', account_id=account_id)