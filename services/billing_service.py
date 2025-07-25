# services/billing_service.py
import logging
import time
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from threading import Thread
from concurrent.futures import ThreadPoolExecutor, as_completed
from models import db, ServiceAccount, Project, BillingAccount, BillingOperation
from flask import current_app
import json
from datetime import datetime


def remove_project_admin_rights(project_id, service_account_id):
    """解除服务账号对项目的Admin权限"""
    try:
        # 获取服务账号信息
        service_account_obj = ServiceAccount.query.get(service_account_id)
        if not service_account_obj:
            return False, "找不到指定的服务账号"
        
        # 获取项目信息
        project = Project.query.filter_by(
            project_id=project_id, 
            service_account_id=service_account_id
        ).first()
        
        if not project:
            return False, "找不到指定的项目"
        
        # 获取凭证
        credentials = service_account.Credentials.from_service_account_file(
            service_account_obj.credentials_file,
            scopes=['https://www.googleapis.com/auth/cloud-platform'])
        
        # 调用Google API解除项目的权限
        success = remove_project_admin_permission(
            credentials, 
            project_id, 
            service_account_obj.email
        )
        
        if success:
            # 记录操作
            operation = BillingOperation(
                operation_type='remove_project_permission',
                service_account_id=service_account_id,
                project_id=project_id,
                old_value="project.admin",
                new_value="removed",
                status='success',
                message=f"成功解除项目Admin权限"
            )
            db.session.add(operation)
            db.session.commit()
            return True, "成功解除项目Admin权限"
        else:
            # 记录操作
            operation = BillingOperation(
                operation_type='remove_project_permission',
                service_account_id=service_account_id,
                project_id=project_id,
                old_value="project.admin",
                new_value="failed",
                status='failed',
                message=f"解除项目Admin权限失败"
            )
            db.session.add(operation)
            db.session.commit()
            return False, "解除项目Admin权限失败"
    except Exception as e:
        # 记录操作
        try:
            operation = BillingOperation(
                operation_type='remove_project_permission',
                service_account_id=service_account_id,
                project_id=project_id,
                old_value="project.admin",
                new_value="failed",
                status='failed',
                message=str(e)
            )
            db.session.add(operation)
            db.session.commit()
        except:
            db.session.rollback()
        
        return False, f"解除权限过程中发生错误: {str(e)}"

def remove_project_admin_permission(credentials, project_id, service_account_email):
    """
    将指定服务账号从项目的 roles/owner 绑定中移除
    """
    service = build_service_with_cache('cloudresourcemanager', 'v1', credentials)
    target_member = f"serviceAccount:{service_account_email}"
    
    try:
        # 1. 读取当前策略
        policy = service.projects().getIamPolicy(
            resource=project_id,
            body={}
        ).execute()
        
        bindings = policy.get("bindings", [])
        updated = False
        new_bindings = []
        
        # 2. 遍历并移除目标成员
        admin_roles = ["roles/owner", "roles/editor", "roles/resourcemanager.projectIamAdmin"]
        
        for b in bindings:
            if b.get("role") in admin_roles and target_member in b.get("members", []):
                members = [m for m in b["members"] if m != target_member]
                if members:
                    b["members"] = members
                    new_bindings.append(b)
                # 如果 members 为空则跳过此绑定
                updated = True
            else:
                new_bindings.append(b)
        
        if not updated:
            logging.info("目标服务账号不在项目的管理员角色中，无需修改")
            return True
        
        # 3. 写回策略
        policy["bindings"] = new_bindings
        response = service.projects().setIamPolicy(
            resource=project_id,
            body={"policy": policy}
        ).execute()
        
        logging.info(f"已成功移除 {target_member} 在项目 {project_id} 上的管理员权限")
        return True
    
    except HttpError as e:
        logging.error(f"移除项目权限失败: {e}")
        return False



def build_service_with_cache(serviceName, version, credentials):
    """创建Google API服务客户端，禁用文件缓存"""
    return build(
        serviceName, 
        version, 
        credentials=credentials,
        cache_discovery=False  # 禁用文件缓存
    )

def get_projects(credentials):
    service = build_service_with_cache('cloudresourcemanager', 'v1', credentials)
    request = service.projects().list()
    projects = []
    while request is not None:
        try:
            response = request.execute()
            for project in response.get('projects', []):
                projects.append(project['projectId'])
            request = service.projects().list_next(previous_request=request, previous_response=response)
        except HttpError as e:
            logging.error(f"Error listing projects: {e}")
            break
    return projects

def get_billing_accounts(credentials):
    service = build_service_with_cache('cloudbilling', 'v1', credentials)
    request = service.billingAccounts().list()
    billing_accounts = []
    while request is not None:
        try:
            response = request.execute()
            for account in response.get('billingAccounts', []):
                billing_accounts.append({
                    'name': account['name'],
                    'displayName': account['displayName'],
                    'open': account['open']
                })
            request = service.billingAccounts().list_next(previous_request=request, previous_response=response)
        except HttpError as e:
            logging.error(f"Error listing billing accounts: {e}")
            break
    return billing_accounts

def get_project_billing_info(cloudbilling, project_id):
    try:
        return cloudbilling.projects().getBillingInfo(name=f'projects/{project_id}').execute()
    except HttpError as e:
        logging.error(f"Error getting billing info for project {project_id}: {e}")
        return None

def update_project_billing_info(cloudbilling, project_id, billing_account_name):
    try:
        body = {"billingAccountName": billing_account_name}
        result = cloudbilling.projects().updateBillingInfo(name=f'projects/{project_id}', body=body).execute()
        logging.info(f"Updated billing account for project {project_id} to {billing_account_name}")
        return result
    except HttpError as e:
        logging.error(f"Error updating billing info for project {project_id}: {e}")
        raise

def remove_billing_admin_permission(credentials, billing_account_name, service_account_email):
    """
    将指定服务账号从某个 Billing Account 的 roles/billing.admin 绑定中移除
    """
    service = build_service_with_cache('cloudbilling', 'v1', credentials)
    billing_account_id = billing_account_name.split('/')[-1]
    resource = f"billingAccounts/{billing_account_id}"
    target_member = f"serviceAccount:{service_account_email}"
    try:
        # 1. 读取当前策略
        policy = service.billingAccounts().getIamPolicy(
            resource=resource
        ).execute()
        bindings = policy.get("bindings", [])
        updated = False
        new_bindings = []
        # 2. 遍历并移除目标成员
        for b in bindings:
            if b.get("role") == "roles/billing.admin" and target_member in b.get("members", []):
                members = [m for m in b["members"] if m != target_member]
                if members:
                    b["members"] = members
                    new_bindings.append(b)
                # 如果 members 为空且您想整个绑定都删掉，则直接跳过
                updated = True
            else:
                new_bindings.append(b)
        if not updated:
            logging.info("目标服务账号不在 roles/billing.admin 中，无需修改")
            return True
        # 3. 写回策略（需带 etag）
        policy["bindings"] = new_bindings
        response = service.billingAccounts().setIamPolicy(
            resource=resource,
            body={"policy": policy}
        ).execute()
        logging.info(f"已成功移除 {target_member} 在账单 {billing_account_id} 上的 Billing Admin 权限")
        return True
    except HttpError as e:
        logging.error(f"移除失败: {e}")
        return False

def get_service_account_email(credentials_file):
    """从凭证文件获取服务账号邮箱"""
    try:
        with open(credentials_file, 'r') as f:
            creds_data = json.load(f)
            return creds_data.get('client_email')
    except Exception as e:
        logging.error(f"无法从凭证文件获取服务账号邮箱: {e}")
        return None

def process_account(app, gcp_account):
    with app.app_context():
        try:
            credentials_file = gcp_account['credentials_file']
            credentials = service_account.Credentials.from_service_account_file(
                credentials_file,
                scopes=['https://www.googleapis.com/auth/cloud-platform'])
            
            # 获取服务账号邮箱
            service_account_email = get_service_account_email(credentials_file)
            
            # 在数据库中查找或创建服务账号记录
            sa_obj = ServiceAccount.query.filter_by(name=gcp_account['name']).first()
            if not sa_obj:
                sa_obj = ServiceAccount(
                    name=gcp_account['name'],
                    email=service_account_email,
                    credentials_file=credentials_file
                )
                db.session.add(sa_obj)
                db.session.commit()
            
            # 获取项目和账单信息
            projects = get_projects(credentials)
            billing_accounts = get_billing_accounts(credentials)
            cloudbilling = build_service_with_cache('cloudbilling', 'v1', credentials)
            
            # 处理账单账户信息
            billing_accounts_dict = {account['name']: account for account in billing_accounts}
            active_billing_accounts = [account['name'] for account in billing_accounts if account['open']]
            
            # 更新数据库中的账单账户信息
            for account in billing_accounts:
                db_account = BillingAccount.query.filter_by(
                    name=account['name'], 
                    service_account_id=sa_obj.id
                ).first()
                
                if not db_account:
                    db_account = BillingAccount(
                        name=account['name'],
                        display_name=account['displayName'],
                        account_id=account['name'].split('/')[-1],
                        is_open=account['open'],
                        is_used=False,
                        service_account_id=sa_obj.id
                    )
                    db.session.add(db_account)
                else:
                    db_account.display_name = account['displayName']
                    db_account.is_open = account['open']
            
            db.session.commit()
            
            # 处理项目信息
            used_billing_accounts = set()
            
            for project_id in projects:
                billing_info = get_project_billing_info(cloudbilling, project_id)
                if billing_info:
                    current_billing_account = billing_info.get('billingAccountName', 'None')
                    
                    # 获取 displayName
                    display_name = 'None'
                    if current_billing_account != 'None':
                        account_info_temp = billing_accounts_dict.get(current_billing_account, {})
                        display_name = account_info_temp.get('displayName', current_billing_account)
                    
                    # 检查当前账单是否失效
                    if current_billing_account != 'None' and current_billing_account not in active_billing_accounts:
                        # 尝试更换到有效账单
                        for new_account in active_billing_accounts:
                            try:
                                old_billing_account = current_billing_account
                                update_project_billing_info(cloudbilling, project_id, new_account)
                                current_billing_account = new_account
                                display_name = billing_accounts_dict.get(new_account, {}).get('displayName', new_account)
                                
                                # 记录操作
                                operation = BillingOperation(
                                    operation_type='update',
                                    service_account_id=sa_obj.id,
                                    project_id=project_id,
                                    billing_account_id=new_account.split('/')[-1],
                                    old_value=old_billing_account,
                                    new_value=new_account,
                                    status='success',
                                    message=f"从失效账单切换到有效账单"
                                )
                                db.session.add(operation)
                                break
                            except Exception as e:
                                logging.error(f"Failed to switch project {project_id} to billing account {new_account}: {e}")
                                operation = BillingOperation(
                                    operation_type='update',
                                    service_account_id=sa_obj.id,
                                    project_id=project_id,
                                    billing_account_id=new_account.split('/')[-1],
                                    old_value=current_billing_account,
                                    new_value=new_account,
                                    status='failed',
                                    message=str(e)
                                )
                                db.session.add(operation)
                        
                        if current_billing_account not in active_billing_accounts:
                            # 如果无法更换到有效账单，解绑当前账单
                            try:
                                old_billing_account = current_billing_account
                                update_project_billing_info(cloudbilling, project_id, '')
                                current_billing_account = 'None'
                                display_name = 'None'
                                
                                # 记录操作
                                operation = BillingOperation(
                                    operation_type='unbind',
                                    service_account_id=sa_obj.id,
                                    project_id=project_id,
                                    billing_account_id=old_billing_account.split('/')[-1],
                                    old_value=old_billing_account,
                                    new_value='None',
                                    status='success',
                                    message=f"解绑失效账单"
                                )
                                db.session.add(operation)
                            except Exception as e:
                                logging.error(f"Failed to unbind billing account for project {project_id}: {e}")
                                operation = BillingOperation(
                                    operation_type='unbind',
                                    service_account_id=sa_obj.id,
                                    project_id=project_id,
                                    billing_account_id=current_billing_account.split('/')[-1],
                                    old_value=current_billing_account,
                                    new_value='None',
                                    status='failed',
                                    message=str(e)
                                )
                                db.session.add(operation)
                    
                    # 添加账单到已使用集合
                    if current_billing_account != 'None':
                        used_billing_accounts.add(current_billing_account)
                    
                    # 更新项目信息
                    db_project = Project.query.filter_by(
                        project_id=project_id, 
                        service_account_id=sa_obj.id
                    ).first()
                    
                    if not db_project:
                        db_project = Project(
                            project_id=project_id,
                            service_account_id=sa_obj.id,
                            billing_account_id=current_billing_account.split('/')[-1] if current_billing_account != 'None' else None,
                            billing_account_name=current_billing_account,
                            billing_account_display_name=display_name
                        )
                        db.session.add(db_project)
                    else:
                        db_project.billing_account_id = current_billing_account.split('/')[-1] if current_billing_account != 'None' else None
                        db_project.billing_account_name = current_billing_account
                        db_project.billing_account_display_name = display_name
            
            # 更新账单使用状态
            for db_account in BillingAccount.query.filter_by(service_account_id=sa_obj.id).all():
                db_account.is_used = db_account.name in used_billing_accounts
            
            db.session.commit()
            
            logging.info(f"Successfully processed account {gcp_account['name']}")
            return True
            
        except Exception as e:
            logging.error(f"Error processing account {gcp_account['name']}: {str(e)}", exc_info=True)
            return False

def update_project_status(app):
    """定期更新项目状态的后台任务"""
    while True:
        start_time = time.time()
        
        with app.app_context():
            gcp_accounts = app.config['GCP_ACCOUNTS']
            
            with ThreadPoolExecutor(max_workers=min(32, len(gcp_accounts))) as executor:
                future_to_account = {executor.submit(process_account, app, account): account for account in gcp_accounts}
                for future in as_completed(future_to_account):
                    account = future_to_account[future]
                    try:
                        future.result()
                    except Exception as e:
                        logging.error(f"Error in processing account {account['name']}: {e}")
        
        execution_time = time.time() - start_time
        sleep_time = max(0, 300 - execution_time)  # 确保不会睡眠负数时间
        logging.info(f"Update completed in {execution_time:.2f} seconds. Sleeping for {sleep_time:.2f} seconds.")
        time.sleep(sleep_time)

def delete_billing_account_record(billing_id, service_account_id):
    """从数据库中删除账单账户记录"""
    try:
        # 将服务账号ID转为整数
        service_account_id = int(service_account_id)
        
        # 查找对应的账单记录
        billing_account = BillingAccount.query.filter_by(
            account_id=billing_id, 
            service_account_id=service_account_id
        ).first()
        
        if not billing_account:
            return False, "未找到对应的账单记录"
        
        # 检查是否有项目使用此账单
        projects_using_billing = Project.query.filter_by(
            billing_account_id=billing_id,
            service_account_id=service_account_id
        ).all()
        
        if projects_using_billing:
            return False, f"有 {len(projects_using_billing)} 个项目正在使用此账单，无法删除"
        
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
        
        return True, "账单记录已成功删除"
        
    except Exception as e:
        logging.error(f"删除账单账户记录失败: {str(e)}")
        db.session.rollback()
        return False, str(e)

def remove_billing_admin_rights(billing_account_name, service_account_id):
    """解除服务账号对账单的Billing Admin权限"""
    try:
        # 获取服务账号信息
        service_account_obj = ServiceAccount.query.get(service_account_id)
        if not service_account_obj:
            return False, "找不到指定的服务账号"
        
        # 获取凭证
        credentials = service_account.Credentials.from_service_account_file(
            service_account_obj.credentials_file,
            scopes=['https://www.googleapis.com/auth/cloud-platform'])
        
        # 解除权限
        success = remove_billing_admin_permission(
            credentials, 
            billing_account_name, 
            service_account_obj.email
        )
        
        if success:
            # 记录操作
            operation = BillingOperation(
                operation_type='remove_permission',
                service_account_id=service_account_id,
                billing_account_id=billing_account_name.split('/')[-1],
                old_value="billing.admin",
                new_value="removed",
                status='success',
                message=f"成功解除Billing Admin权限"
            )
            db.session.add(operation)
            db.session.commit()
            return True, "成功解除Billing Admin权限"
        else:
            # 记录操作
            operation = BillingOperation(
                operation_type='remove_permission',
                service_account_id=service_account_id,
                billing_account_id=billing_account_name.split('/')[-1],
                old_value="billing.admin",
                new_value="failed",
                status='failed',
                message=f"解除Billing Admin权限失败"
            )
            db.session.add(operation)
            db.session.commit()
            return False, "解除Billing Admin权限失败"
    except Exception as e:
        # 记录操作
        try:
            operation = BillingOperation(
                operation_type='remove_permission',
                service_account_id=service_account_id,
                billing_account_id=billing_account_name.split('/')[-1] if billing_account_name else "unknown",
                old_value="billing.admin",
                new_value="failed",
                status='failed',
                message=str(e)
            )
            db.session.add(operation)
            db.session.commit()
        except:
            db.session.rollback()
        
        return False, f"解除权限过程中发生错误: {str(e)}"

def unbind_project_billing(project_id, service_account_id):
    """解绑项目的账单信息"""
    try:
        # 获取服务账号信息
        service_account_obj = ServiceAccount.query.get(service_account_id)
        if not service_account_obj:
            return False, "找不到指定的服务账号"
        
        # 获取项目信息
        project = Project.query.filter_by(
            project_id=project_id, 
            service_account_id=service_account_id
        ).first()
        
        if not project:
            return False, "找不到指定的项目"
        
        # 如果项目没有关联账单，直接返回成功
        if not project.billing_account_id:
            return True, "项目已经没有关联账单"
        
        old_billing_account_name = project.billing_account_name
        old_billing_account_id = project.billing_account_id
        
        # 获取凭证
        credentials = service_account.Credentials.from_service_account_file(
            service_account_obj.credentials_file,
            scopes=['https://www.googleapis.com/auth/cloud-platform'])
        
        # 创建Cloud Billing服务
        cloudbilling = build_service_with_cache('cloudbilling', 'v1', credentials)
        
        try:
            # 解绑账单
            update_project_billing_info(cloudbilling, project_id, '')
            
            # 更新项目信息
            project.billing_account_id = None
            project.billing_account_name = 'None'
            project.billing_account_display_name = 'None'
            
            # 记录操作
            operation = BillingOperation(
                operation_type='unbind',
                service_account_id=service_account_id,
                project_id=project_id,
                billing_account_id=old_billing_account_id,
                old_value=old_billing_account_name,
                new_value='None',
                status='success',
                message="手动解绑项目账单"
            )
            db.session.add(operation)
            db.session.commit()
            
            return True, "成功解绑项目账单"
        except Exception as e:
            db.session.rollback()
            
            # 记录操作
            operation = BillingOperation(
                operation_type='unbind',
                service_account_id=service_account_id,
                project_id=project_id,
                billing_account_id=old_billing_account_id,
                old_value=old_billing_account_name,
                new_value='None',
                status='failed',
                message=str(e)
            )
            db.session.add(operation)
            db.session.commit()
            
            return False, f"解绑项目账单失败: {str(e)}"
    
    except Exception as e:
        logging.error(f"解绑项目账单过程中发生错误: {str(e)}")
        return False, f"解绑过程中发生错误: {str(e)}"