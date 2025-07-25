from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# 导入模型类
from .models import ServiceAccount, Project, BillingAccount, BillingOperation

# 导出这些类，使它们可以通过 models 包直接访问
__all__ = ['db', 'ServiceAccount', 'Project', 'BillingAccount', 'BillingOperation']