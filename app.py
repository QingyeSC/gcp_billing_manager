import logging
import os
import time
from threading import Thread
from flask import Flask
from dotenv import load_dotenv
import warnings
import google.auth.transport.requests

# 禁用特定的警告
warnings.filterwarnings("ignore", "Your application has authenticated using end user credentials")
warnings.filterwarnings("ignore", "file_cache is only supported with oauth2client<4.0.0")

# 修改 google.auth.transport.requests 模块的 _LOGGER
google.auth.transport.requests._LOGGER.setLevel(logging.ERROR)

# 禁用googleapiclient的文件缓存警告
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def create_app():
    # 加载 .env 文件
    load_dotenv()
    
    app = Flask(__name__, 
                static_folder='static',
                template_folder='templates')
    
    # 配置数据库
    app.config['SQLALCHEMY_DATABASE_URI'] = f"mysql+pymysql://{os.getenv('MYSQL_USER')}:{os.getenv('MYSQL_PASSWORD')}@{os.getenv('MYSQL_HOST')}/{os.getenv('MYSQL_DB')}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    # 配置GCP账户信息
    app.config['GCP_ACCOUNT_NAMES'] = os.getenv('GCP_ACCOUNT_NAMES', '').split(',')
    app.config['GCP_ACCOUNTS'] = [
        {'name': name, 'credentials_file': f'/app/credentials/{name}.json'}
        for name in app.config['GCP_ACCOUNT_NAMES']
    ]
    
    # 初始化数据库
    from models import db
    db.init_app(app)
    
    # 注册路由
    from routes.api import api_bp
    from routes.web import web_bp
    
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(web_bp)
    
    # 创建数据库表
    with app.app_context():
        db.create_all()
    
    return app

def start_update_thread(app):
    from services.billing_service import update_project_status
    
    update_thread = Thread(target=update_project_status, args=(app,))
    update_thread.daemon = True
    update_thread.start()
    logging.info("Update thread started")

def main():
    logging.info("Starting GCP Billing Monitor")
    app = create_app()
    start_update_thread(app)
    app.run(host='0.0.0.0', port=8848)

if __name__ == "__main__":
    main()