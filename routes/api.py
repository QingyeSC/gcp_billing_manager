# routes/api.py
from flask import Blueprint, jsonify, request, current_app
from models import db, ServiceAccount, Project, BillingAccount, BillingOperation
from services.billing_service import delete_billing_account_record, remove_billing_admin_rights, unbind_project_billing
import logging

api_bp = Blueprint('api', __name__)

@api_bp.route('/service-accounts', methods=['GET'])
def get_service_accounts():
    """获取所有服务账号信息"""
    try:
        accounts = ServiceAccount.query.all()
        result = []
        
        for account in accounts:
            # 统计账号下的项目和账单信息
            projects = Project.query.filter_by(service_account_id=account.id).all()
            inactive_billing_accounts = BillingAccount.query.filter_by(
                service_account_id=account.id, 
                is_open=False
            ).all()
            active_billing_accounts = BillingAccount.query.filter_by(
                service_account_id=account.id, 
                is_open=True
            ).all()
            
            result.append({
                'id': account.id,
                'name': account.name,
                'email': account.email,
                'project_count': len(projects),
                'inactive_billing_count': len(inactive_billing_accounts),
                'active_billing_count': len(active_billing_accounts)
            })
        
        return jsonify({
            'status': 'success',
            'data': result
        })
    
    except Exception as e:
        logging.error(f"Error getting service accounts: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@api_bp.route('/service-accounts/<int:account_id>', methods=['GET'])
def get_service_account_details(account_id):
    """获取特定服务账号的详细信息"""
    try:
        account = ServiceAccount.query.get(account_id)
        if not account:
            return jsonify({
                'status': 'error',
                'message': '服务账号未找到'
            }), 404
        
        projects = Project.query.filter_by(service_account_id=account_id).all()
        projects_data = [project.to_dict() for project in projects]
        
        active_billing_accounts = BillingAccount.query.filter_by(
            service_account_id=account_id, 
            is_open=True
        ).all()
        active_billing_data = [account.to_dict() for account in active_billing_accounts]
        
        inactive_billing_accounts = BillingAccount.query.filter_by(
            service_account_id=account_id, 
            is_open=False
        ).all()
        inactive_billing_data = [account.to_dict() for account in inactive_billing_accounts]
        
        # 获取最近操作记录
        operations = BillingOperation.query.filter_by(
            service_account_id=account_id
        ).order_by(BillingOperation.created_at.desc()).limit(20).all()
        operations_data = [op.to_dict() for op in operations]
        
        return jsonify({
            'status': 'success',
            'data': {
                'account': {
                    'id': account.id,
                    'name': account.name,
                    'email': account.email
                },
                'projects': projects_data,
                'active_billing_accounts': active_billing_data,
                'inactive_billing_accounts': inactive_billing_data,
                'recent_operations': operations_data
            }
        })
    
    except Exception as e:
        logging.error(f"获取服务账号详情失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@api_bp.route('/projects', methods=['GET'])
def get_projects():
    """获取所有项目信息"""
    try:
        account_id = request.args.get('account_id')
        if account_id:
            projects = Project.query.filter_by(service_account_id=account_id).all()
        else:
            projects = Project.query.all()
        
        return jsonify({
            'status': 'success',
            'data': [project.to_dict() for project in projects]
        })
    
    except Exception as e:
        logging.error(f"获取项目列表失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

# routes/api.py (修改项目相关API)

@api_bp.route('/projects/<string:project_id>', methods=['DELETE'])
def delete_project(project_id):
    """从系统中删除项目记录"""
    try:
        service_account_id = request.args.get('service_account_id')
        if not service_account_id:
            return jsonify({
                'status': 'error',
                'message': '缺少必要参数 service_account_id'
            }), 400
        
        service_account_id = int(service_account_id)  # 转换为整数
        
        # 查找对应的项目记录
        project = Project.query.filter_by(
            project_id=project_id, 
            service_account_id=service_account_id
        ).first()
        
        if not project:
            return jsonify({
                'status': 'error',
                'message': '未找到对应的项目记录'
            }), 404
        
        # 检查服务账号是否还拥有该项目的管理员权限
        service_account_obj = ServiceAccount.query.get(service_account_id)
        if not service_account_obj:
            return jsonify({
                'status': 'error',
                'message': '找不到指定的服务账号'
            }), 404
            
        # 检查最近是否已经解除了权限
        recent_permission_removal = BillingOperation.query.filter_by(
            operation_type='remove_project_permission',
            service_account_id=service_account_id,
            project_id=project_id,
            status='success'
        ).order_by(BillingOperation.created_at.desc()).first()
        
        if not recent_permission_removal:
            return jsonify({
                'status': 'error',
                'message': '删除前必须先解除服务账号对该项目的管理员权限'
            }), 400
        
        # 记录操作
        operation = BillingOperation(
            operation_type='delete_project',
            service_account_id=service_account_id,
            project_id=project_id,
            status='success',
            message='从系统中删除项目记录'
        )
        db.session.add(operation)
        
        # 删除项目记录
        db.session.delete(project)
        db.session.commit()
        
        return jsonify({
            'status': 'success',
            'message': '项目记录已成功删除'
        })
    
    except Exception as e:
        logging.error(f"删除项目记录失败: {str(e)}")
        db.session.rollback()
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@api_bp.route('/projects/<string:project_id>/admin-rights', methods=['DELETE'])
def remove_project_admin_rights_api(project_id):
    """解除服务账号对项目的管理员权限"""
    try:
        service_account_id = request.args.get('service_account_id')
        if not service_account_id:
            return jsonify({
                'status': 'error',
                'message': '缺少必要参数 service_account_id'
            }), 400
        
        from services.billing_service import remove_project_admin_rights
        
        success, message = remove_project_admin_rights(project_id, service_account_id)
        
        if success:
            return jsonify({
                'status': 'success',
                'message': message
            })
        else:
            return jsonify({
                'status': 'error',
                'message': message
            }), 400
    
    except Exception as e:
        logging.error(f"解除项目管理员权限失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@api_bp.route('/projects/<string:project_id>/billing', methods=['DELETE'])
def unbind_project_billing_api(project_id):
    """解绑项目的账单信息"""
    try:
        service_account_id = request.args.get('service_account_id')
        if not service_account_id:
            return jsonify({
                'status': 'error',
                'message': '缺少必要参数 service_account_id'
            }), 400
        
        service_account_id = int(service_account_id)  # 转换为整数
        
        # 获取项目信息
        project = Project.query.filter_by(
            project_id=project_id, 
            service_account_id=service_account_id
        ).first()
        
        if not project:
            return jsonify({
                'status': 'error',
                'message': '未找到对应的项目记录'
            }), 404
        
        # 如果项目没有关联账单，直接返回成功
        if not project.billing_account_id:
            return jsonify({
                'status': 'success',
                'message': '项目已经没有关联账单'
            })
        
        # 获取服务账号信息
        service_account_obj = ServiceAccount.query.get(service_account_id)
        if not service_account_obj:
            return jsonify({
                'status': 'error',
                'message': '找不到指定的服务账号'
            }), 404
        
        # 调用解绑账单服务
        success, message = unbind_project_billing(project_id, service_account_id)
        
        if success:
            return jsonify({
                'status': 'success',
                'message': message
            })
        else:
            return jsonify({
                'status': 'error',
                'message': message
            }), 400
    
    except Exception as e:
        logging.error(f"解绑项目账单失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@api_bp.route('/billing-accounts', methods=['GET'])
def get_billing_accounts():
    """获取账单账户信息"""
    try:
        account_id = request.args.get('account_id')
        is_open = request.args.get('is_open')
        
        query = BillingAccount.query
        
        if account_id:
            query = query.filter_by(service_account_id=account_id)
        
        if is_open is not None:
            is_open_bool = is_open.lower() == 'true'
            query = query.filter_by(is_open=is_open_bool)
        
        billing_accounts = query.all()
        
        return jsonify({
            'status': 'success',
            'data': [account.to_dict() for account in billing_accounts]
        })
    
    except Exception as e:
        logging.error(f"获取账单账户列表失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@api_bp.route('/billing-accounts/<string:billing_id>', methods=['DELETE'])
def delete_billing_account(billing_id):
    """删除账单账户记录"""
    try:
        service_account_id = request.args.get('service_account_id')
        if not service_account_id:
            return jsonify({
                'status': 'error',
                'message': '缺少必要参数 service_account_id'
            }), 400
        
        service_account_id = int(service_account_id)  # 转换为整数
        
        # 查找对应的账单记录
        billing_account = BillingAccount.query.filter_by(
            account_id=billing_id, 
            service_account_id=service_account_id
        ).first()
        
        if not billing_account:
            return jsonify({
                'status': 'error',
                'message': '未找到对应的账单记录'
            }), 404
        
        # 检查是否有项目使用此账单
        projects_using_billing = Project.query.filter_by(
            billing_account_id=billing_id,
            service_account_id=service_account_id
        ).all()
        
        if projects_using_billing:
            return jsonify({
                'status': 'error',
                'message': f'有 {len(projects_using_billing)} 个项目正在使用此账单，无法删除'
            }), 400
        
        # 记录操作
        operation = BillingOperation(
            operation_type='delete_billing',
            service_account_id=service_account_id,
            billing_account_id=billing_id,
            status='success',
            message='从系统中删除账单记录'
        )
        db.session.add(operation)
        
        # 删除账单记录
        db.session.delete(billing_account)
        db.session.commit()
        
        return jsonify({
            'status': 'success',
            'message': '账单记录已成功删除'
        })
    
    except Exception as e:
        logging.error(f"删除账单账户记录失败: {str(e)}")
        db.session.rollback()
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@api_bp.route('/billing-accounts/<string:billing_id>/admin-rights', methods=['DELETE'])
def remove_admin_rights(billing_id):
    """解除账单账户的Billing Admin权限"""
    try:
        service_account_id = request.args.get('service_account_id')
        if not service_account_id:
            return jsonify({
                'status': 'error',
                'message': '缺少必要参数 service_account_id'
            }), 400
        
        # 构建账单账户全名
        billing_account_name = f"billingAccounts/{billing_id}"
        
        success, message = remove_billing_admin_rights(billing_account_name, service_account_id)
        
        if success:
            return jsonify({
                'status': 'success',
                'message': message
            })
        else:
            return jsonify({
                'status': 'error',
                'message': message
            }), 400
    
    except Exception as e:
        logging.error(f"解除Billing Admin权限失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@api_bp.route('/operations', methods=['GET'])
def get_operations():
    """获取操作记录"""
    try:
        account_id = request.args.get('account_id')
        operation_type = request.args.get('type')
        
        query = BillingOperation.query
        
        if account_id:
            query = query.filter_by(service_account_id=account_id)
        
        if operation_type:
            query = query.filter_by(operation_type=operation_type)
        
        # 按时间倒序排列
        query = query.order_by(BillingOperation.created_at.desc())
        
        # 限制返回数量
        limit = request.args.get('limit', 50, type=int)
        operations = query.limit(limit).all()
        
        return jsonify({
            'status': 'success',
            'data': [op.to_dict() for op in operations]
        })
    
    except Exception as e:
        logging.error(f"获取操作记录失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@api_bp.route('/status', methods=['GET'])
def get_status():
    """获取系统状态概览"""
    try:
        # 统计信息
        service_account_count = ServiceAccount.query.count()
        project_count = Project.query.count()
        active_billing_count = BillingAccount.query.filter_by(is_open=True).count()
        inactive_billing_count = BillingAccount.query.filter_by(is_open=False).count()
        
        # 最近操作
        recent_operations = BillingOperation.query.order_by(
            BillingOperation.created_at.desc()
        ).limit(5).all()
        
        return jsonify({
            'status': 'success',
            'data': {
                'counts': {
                    'service_accounts': service_account_count,
                    'projects': project_count,
                    'active_billing_accounts': active_billing_count,
                    'inactive_billing_accounts': inactive_billing_count
                },
                'recent_operations': [op.to_dict() for op in recent_operations]
            }
        })
    
    except Exception as e:
        logging.error(f"获取系统状态失败: {str(e)}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500