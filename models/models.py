# models/models.py
from datetime import datetime
from . import db

class ServiceAccount(db.Model):
    __tablename__ = 'service_accounts'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    email = db.Column(db.String(200), nullable=False)
    credentials_file = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    projects = db.relationship('Project', backref='account', lazy=True, cascade="all, delete-orphan")
    billing_accounts = db.relationship('BillingAccount', backref='account', lazy=True, cascade="all, delete-orphan")

class Project(db.Model):
    __tablename__ = 'projects'
    
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.String(100), nullable=False)
    service_account_id = db.Column(db.Integer, db.ForeignKey('service_accounts.id'), nullable=False)
    billing_account_id = db.Column(db.String(100), nullable=True)
    billing_account_name = db.Column(db.String(200), nullable=True)
    billing_account_display_name = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'billing_account_id': self.billing_account_id,
            'billing_account_name': self.billing_account_name,
            'billing_account_display_name': self.billing_account_display_name,
            'service_account_id': self.service_account_id,
            'updated_at': self.updated_at.isoformat()
        }

class BillingAccount(db.Model):
    __tablename__ = 'billing_accounts'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    display_name = db.Column(db.String(200), nullable=True)
    account_id = db.Column(db.String(100), nullable=False)
    is_open = db.Column(db.Boolean, default=True)
    is_used = db.Column(db.Boolean, default=False)
    service_account_id = db.Column(db.Integer, db.ForeignKey('service_accounts.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'account_id': self.account_id,
            'is_open': self.is_open,
            'is_used': self.is_used,
            'service_account_id': self.service_account_id,
            'updated_at': self.updated_at.isoformat()
        }

class BillingOperation(db.Model):
    __tablename__ = 'billing_operations'
    
    id = db.Column(db.Integer, primary_key=True)
    operation_type = db.Column(db.String(50), nullable=False)  # e.g., 'update', 'remove_permission'
    service_account_id = db.Column(db.Integer, db.ForeignKey('service_accounts.id'), nullable=False)
    project_id = db.Column(db.String(100), nullable=True)
    billing_account_id = db.Column(db.String(100), nullable=True)
    old_value = db.Column(db.String(300), nullable=True)
    new_value = db.Column(db.String(300), nullable=True)
    status = db.Column(db.String(50), nullable=False)  # 'success', 'failed'
    message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        return {
            'id': self.id,
            'operation_type': self.operation_type,
            'service_account_id': self.service_account_id,
            'project_id': self.project_id,
            'billing_account_id': self.billing_account_id,
            'old_value': self.old_value,
            'new_value': self.new_value,
            'status': self.status,
            'message': self.message,
            'created_at': self.created_at.isoformat()
        }