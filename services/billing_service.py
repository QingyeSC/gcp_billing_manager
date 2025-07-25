# services/billing_service.py - 生产级优化版本 v2.1 (兼容性修复)
import logging
import time
import math
import os
import random
import json
import threading
import inspect
from datetime import datetime
from collections import defaultdict
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple, Any, Union
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.api_core import exceptions as google_exceptions
from threading import Thread, Semaphore, Lock
from flask import current_app
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy import create_engine

from models import db, ServiceAccount, Project, BillingAccount, BillingOperation

# ==================== 配置管理 ====================

@dataclass
class BillingConfig:
    """账单服务配置类"""
    max_projects_per_billing: int = 3
    update_interval: int = 300
    batch_size: int = 10
    max_retries: int = 3
    enable_auto_switch: bool = True
    max_workers: int = 8
    task_timeout: int = 600
    max_qps_per_account: int = 10
    base_retry_delay: float = 1.0
    max_retry_delay: float = 60.0
    enable_jitter: bool = True

    @classmethod
    def from_env(cls) -> 'BillingConfig':
        """从环境变量加载配置"""
        return cls(
            max_projects_per_billing=int(os.getenv('MAX_PROJECTS_PER_BILLING', 3)),
            update_interval=int(os.getenv('UPDATE_INTERVAL', 300)),
            batch_size=int(os.getenv('BATCH_SIZE', 10)),
            max_retries=int(os.getenv('MAX_RETRIES', 3)),
            enable_auto_switch=os.getenv('ENABLE_AUTO_SWITCH', 'true').lower() == 'true',
            max_workers=int(os.getenv('MAX_WORKERS', 8)),
            task_timeout=int(os.getenv('TASK_TIMEOUT', 600)),
            max_qps_per_account=int(os.getenv('MAX_QPS_PER_ACCOUNT', 10)),
            base_retry_delay=float(os.getenv('BASE_RETRY_DELAY', 1.0)),
            max_retry_delay=float(os.getenv('MAX_RETRY_DELAY', 60.0)),
            enable_jitter=os.getenv('ENABLE_JITTER', 'true').lower() == 'true'
        )

# 全局配置实例
CONFIG = BillingConfig.from_env()

# ==================== QPS 限速器 ====================

class RateLimiter:
    """QPS限速器 - 令牌桶算法"""
    
    def __init__(self, max_qps: int):
        self.max_qps = max_qps
        self.tokens = max_qps
        self.last_update = time.time()
        self.lock = Lock()
    
    def acquire(self, timeout: float = 30.0) -> bool:
        """获取令牌，如果没有令牌则等待"""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            with self.lock:
                now = time.time()
                # 补充令牌
                time_passed = now - self.last_update
                self.tokens = min(self.max_qps, self.tokens + time_passed * self.max_qps)
                self.last_update = now
                
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
            
            # 等待下次尝试
            time.sleep(0.1)
        
        return False

# 全局QPS限速器
_rate_limiters: Dict[str, RateLimiter] = {}
_limiter_lock = Lock()

def get_rate_limiter(service_account_name: str) -> RateLimiter:
    """获取指定服务账号的QPS限速器"""
    with _limiter_lock:
        if service_account_name not in _rate_limiters:
            _rate_limiters[service_account_name] = RateLimiter(CONFIG.max_qps_per_account)
        return _rate_limiters[service_account_name]

# ==================== 数据库会话管理 ====================

@contextmanager
def create_db_session():
    """创建独立的数据库会话，确保线程安全"""
    Session = sessionmaker(bind=db.engine)
    session = Session()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logging.error(f"数据库会话错误: {e}")
        raise
    finally:
        session.close()

# ==================== 改进的重试机制 ====================

def retry_with_exponential_backoff(
    func,
    max_retries: int = CONFIG.max_retries,
    base_delay: float = CONFIG.base_retry_delay,
    max_delay: float = CONFIG.max_retry_delay,
    enable_jitter: bool = CONFIG.enable_jitter
):
    """
    指数退避重试机制 - 增强版，支持jitter
    
    Args:
        func: 要重试的函数
        max_retries: 最大重试次数
        base_delay: 基础延迟时间
        max_delay: 最大延迟时间
        enable_jitter: 是否启用随机化
    """
    for attempt in range(max_retries):
        try:
            return func()
        except (HttpError, google_exceptions.GoogleAPIError) as e:
            # Google API特定错误处理
            if hasattr(e, 'resp') and hasattr(e.resp, 'status'):
                status_code = e.resp.status
            else:
                status_code = getattr(e, 'code', 500)
            
            # 可重试的状态码
            retryable_codes = {403, 409, 412, 429, 500, 502, 503, 504}
            
            if status_code not in retryable_codes or attempt == max_retries - 1:
                logging.error(f"API错误不可重试或达到最大重试次数: {status_code}")
                raise e
            
            # 计算等待时间
            delay = min(base_delay * (2 ** attempt), max_delay)
            
            if enable_jitter:
                # 添加随机化，避免惊群效应
                delay = random.uniform(0, delay)
            
            # 对429做特殊处理
            if status_code == 429:
                delay *= 2  # 速率限制时等待更久
                logging.warning(f"遇到速率限制 (尝试 {attempt + 1}/{max_retries}), 等待 {delay:.2f}s")
            else:
                logging.warning(f"API调用失败 {status_code} (尝试 {attempt + 1}/{max_retries}), 等待 {delay:.2f}s")
            
            time.sleep(delay)
            
        except Exception as e:
            if attempt == max_retries - 1:
                logging.error(f"达到最大重试次数，操作失败: {e}")
                raise e
            
            delay = min(base_delay * (2 ** attempt), max_delay)
            if enable_jitter:
                delay = random.uniform(0, delay)
            
            logging.warning(f"操作失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}, 等待 {delay:.2f}s")
            time.sleep(delay)

# ==================== Google API 客户端管理 ====================

class GoogleAPIClient:
    """Google API客户端管理器"""
    
    def __init__(self, credentials, service_account_name: str):
        self.credentials = credentials
        self.service_account_name = service_account_name
        self.rate_limiter = get_rate_limiter(service_account_name)
        self._services = {}
    
    def get_service(self, service_name: str, version: str):
        """获取Google API服务客户端，支持缓存和连接管理"""
        key = f"{service_name}:{version}"
        if key not in self._services:
            build_kwargs = {
                'serviceName': service_name,
                'version': version,
                'credentials': self.credentials,
                'cache_discovery': False
            }
            
            # 只在支持的版本中添加static_discovery参数
            try:
                # 检查是否支持static_discovery参数
                import inspect
                from googleapiclient.discovery import build as discovery_build
                sig = inspect.signature(discovery_build)
                if 'static_discovery' in sig.parameters:
                    build_kwargs['static_discovery'] = False
            except (ImportError, AttributeError):
                # 老版本不支持，跳过此参数
                pass
            
            self._services[key] = build(**build_kwargs)
        return self._services[key]
    
    def execute_with_rate_limit(self, request, timeout: float = 30.0):
        """执行API请求，带QPS限速"""
        if not self.rate_limiter.acquire(timeout=timeout):
            raise Exception(f"QPS限速超时: {self.service_account_name}")
        
        return request.execute()
    
    def close(self):
        """关闭所有连接"""
        for service in self._services.values():
            if hasattr(service, 'close'):
                try:
                    service.close()
                except:
                    pass
        self._services.clear()

# ==================== v3 API 实现 ====================

def get_projects_v3(api_client: GoogleAPIClient) -> List[str]:
    """获取项目列表 - v3版本，支持搜索和过滤"""
    def _get_projects():
        service = api_client.get_service('cloudresourcemanager', 'v3')
        
        projects = []
        next_page_token = None
        page_size = CONFIG.batch_size * 10
        
        while True:
            # v3 API的正确调用方式：直接传递参数而不是body
            kwargs = {
                'query': 'state:ACTIVE',  # 只获取活跃项目
                'pageSize': page_size
            }
            
            if next_page_token:
                kwargs['pageToken'] = next_page_token
            
            request = service.projects().search(**kwargs)
            response = api_client.execute_with_rate_limit(request)
            
            for project in response.get('projects', []):
                projects.append(project['projectId'])
            
            next_page_token = response.get('nextPageToken')
            if not next_page_token:
                break
        
        return projects
    
    try:
        return retry_with_exponential_backoff(_get_projects)
    except Exception as e:
        logging.error(f"v3 API获取项目列表失败: {e}")
        # 如果v3失败，回退到v1版本
        logging.info("回退到v1 API获取项目列表")
        return get_projects_v1_fallback(api_client)

def get_projects_v1_fallback(api_client: GoogleAPIClient) -> List[str]:
    """回退到v1版本获取项目列表"""
    def _get_projects():
        service = api_client.get_service('cloudresourcemanager', 'v1')
        request = service.projects().list()
        projects = []
        
        while request is not None:
            response = api_client.execute_with_rate_limit(request)
            for project in response.get('projects', []):
                projects.append(project['projectId'])
            request = service.projects().list_next(previous_request=request, previous_response=response)
        
        return projects
    
    try:
        return retry_with_exponential_backoff(_get_projects)
    except Exception as e:
        logging.error(f"v1 API获取项目列表也失败: {e}")
        return []

def get_billing_accounts_v1(api_client: GoogleAPIClient) -> List[Dict[str, Any]]:
    """获取账单账户列表 - 保持v1版本（v3还未支持billing API）"""
    def _get_billing_accounts():
        service = api_client.get_service('cloudbilling', 'v1')
        
        request = service.billingAccounts().list()
        billing_accounts = []
        
        while request:
            response = api_client.execute_with_rate_limit(request)
            
            for account in response.get('billingAccounts', []):
                billing_accounts.append({
                    'name': account['name'],
                    'displayName': account['displayName'],
                    'open': account['open']
                })
            
            request = service.billingAccounts().list_next(
                previous_request=request, 
                previous_response=response
            )
        
        return billing_accounts
    
    try:
        return retry_with_exponential_backoff(_get_billing_accounts)
    except Exception as e:
        logging.error(f"获取账单账户列表失败: {e}")
        return []

def get_project_billing_info_v1(api_client: GoogleAPIClient, project_id: str) -> Optional[Dict[str, Any]]:
    """获取项目账单信息 - v1版本"""
    def _get_billing_info():
        service = api_client.get_service('cloudbilling', 'v1')
        request = service.projects().getBillingInfo(name=f'projects/{project_id}')
        return api_client.execute_with_rate_limit(request)
    
    try:
        return retry_with_exponential_backoff(_get_billing_info)
    except HttpError as e:
        if e.resp.status == 403:
            logging.warning(f"无权限访问项目 {project_id} 的账单信息")
        else:
            logging.error(f"获取项目 {project_id} 账单信息失败: {e}")
        return None
    except Exception as e:
        logging.error(f"获取项目 {project_id} 账单信息时发生异常: {e}")
        return None

def update_project_billing_info_v1(api_client: GoogleAPIClient, project_id: str, billing_account_name: str):
    """更新项目账单信息 - v1版本"""
    def _update_billing_info():
        service = api_client.get_service('cloudbilling', 'v1')
        body = {"billingAccountName": billing_account_name}
        request = service.projects().updateBillingInfo(name=f'projects/{project_id}', body=body)
        result = api_client.execute_with_rate_limit(request)
        logging.info(f"更新项目 {project_id} 账单为 {billing_account_name}")
        return result
    
    return retry_with_exponential_backoff(_update_billing_info)

def remove_project_admin_permission_v3(api_client: GoogleAPIClient, project_id: str, service_account_email: str) -> bool:
    """移除项目管理员权限 - v3版本，支持条件IAM"""
    service = api_client.get_service('cloudresourcemanager', 'v3')
    resource = f'projects/{project_id}'
    target_member = f"serviceAccount:{service_account_email}"
    
    def _update_iam_policy():
        # 1. 获取当前策略（支持条件IAM）
        get_policy_request = service.projects().getIamPolicy(
            resource=resource,
            body={
                'options': {
                    'requestedPolicyVersion': 3  # 支持条件绑定
                }
            }
        )
        policy = api_client.execute_with_rate_limit(get_policy_request)
        
        # 2. 移除目标成员
        bindings = policy.get("bindings", [])
        updated = False
        new_bindings = []
        
        admin_roles = ["roles/owner", "roles/editor", "roles/resourcemanager.projectIamAdmin"]
        
        for binding in bindings:
            if binding.get("role") in admin_roles and target_member in binding.get("members", []):
                members = [m for m in binding["members"] if m != target_member]
                if members:
                    binding["members"] = members
                    new_bindings.append(binding)
                # 如果members为空，则不添加此binding（自动清理）
                updated = True
            else:
                new_bindings.append(binding)
        
        if not updated:
            logging.info("目标服务账号不在项目的管理员角色中，无需修改")
            return True
        
        # 3. 更新策略（使用updateMask提高安全性）
        policy["bindings"] = new_bindings
        set_policy_request = service.projects().setIamPolicy(
            resource=resource,
            body={
                'policy': policy,
                'updateMask': 'bindings,etag'  # 只更新指定字段
            }
        )
        api_client.execute_with_rate_limit(set_policy_request)
        
        logging.info(f"已成功移除 {target_member} 在项目 {project_id} 上的管理员权限")
        return True
    
    try:
        return retry_with_exponential_backoff(_update_iam_policy)
    except Exception as e:
        logging.error(f"移除项目权限失败: {e}")
        return False

def remove_billing_admin_permission_v1(api_client: GoogleAPIClient, billing_account_name: str, service_account_email: str) -> bool:
    """移除账单管理员权限 - v1版本，改进并发安全"""
    service = api_client.get_service('cloudbilling', 'v1')
    billing_account_id = billing_account_name.split('/')[-1]
    resource = f"billingAccounts/{billing_account_id}"
    target_member = f"serviceAccount:{service_account_email}"
    
    def _update_billing_iam_policy():
        # 1. 获取当前策略
        get_policy_request = service.billingAccounts().getIamPolicy(resource=resource)
        policy = api_client.execute_with_rate_limit(get_policy_request)
        
        bindings = policy.get("bindings", [])
        updated = False
        new_bindings = []
        
        # 2. 遍历并移除目标成员
        for binding in bindings:
            if binding.get("role") == "roles/billing.admin" and target_member in binding.get("members", []):
                members = [m for m in binding["members"] if m != target_member]
                if members:
                    binding["members"] = members
                    new_bindings.append(binding)
                # 如果members为空，则不添加此binding（自动清理）
                updated = True
            else:
                new_bindings.append(binding)
        
        if not updated:
            logging.info("目标服务账号不在 roles/billing.admin 中，无需修改")
            return True
        
        # 3. 更新策略（保留etag确保并发安全）
        policy["bindings"] = new_bindings
        set_policy_request = service.billingAccounts().setIamPolicy(
            resource=resource,
            body={"policy": policy}
        )
        api_client.execute_with_rate_limit(set_policy_request)
        
        logging.info(f"已成功移除 {target_member} 在账单 {billing_account_id} 上的 Billing Admin 权限")
        return True
    
    try:
        return retry_with_exponential_backoff(_update_billing_iam_policy)
    except Exception as e:
        logging.error(f"移除账单权限失败: {e}")
        return False

# ==================== 业务逻辑函数 ====================

def get_service_account_email(credentials_file: str) -> Optional[str]:
    """从凭证文件获取服务账号邮箱"""
    try:
        with open(credentials_file, 'r') as f:
            creds_data = json.load(f)
            return creds_data.get('client_email')
    except Exception as e:
        logging.error(f"无法从凭证文件获取服务账号邮箱: {e}")
        return None

def log_operation(
    operation_type: str,
    service_account_id: int,
    project_id: Optional[str] = None,
    billing_account_id: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    status: str = 'success',
    message: str = '',
    session: Optional[Session] = None
):
    """记录操作日志 - 线程安全版本"""
    try:
        # 如果没有传入session，创建新的独立session
        if session is None:
            with create_db_session() as session:
                operation = BillingOperation(
                    operation_type=operation_type,
                    service_account_id=service_account_id,
                    project_id=project_id,
                    billing_account_id=billing_account_id,
                    old_value=old_value,
                    new_value=new_value,
                    status=status,
                    message=message
                )
                session.add(operation)
                # session会自动commit
        else:
            # 使用传入的session
            operation = BillingOperation(
                operation_type=operation_type,
                service_account_id=service_account_id,
                project_id=project_id,
                billing_account_id=billing_account_id,
                old_value=old_value,
                new_value=new_value,
                status=status,
                message=message
            )
            session.add(operation)
            # 不在这里commit，由调用者决定
            
    except Exception as e:
        logging.error(f"记录操作日志失败: {e}")

def get_current_billing_usage(projects_billing_info: Dict[str, str]) -> Dict[str, int]:
    """统计每个账单当前的项目使用数量"""
    usage = defaultdict(int)
    for project_id, billing_name in projects_billing_info.items():
        if billing_name and billing_name != 'None':
            usage[billing_name] += 1
    return dict(usage)

def get_billing_allocation_plan(
    unbound_projects: List[str], 
    active_billings: List[str], 
    current_usage: Dict[str, int]
) -> List[Tuple[str, int]]:
    """制定智能的账单分配计划 - 集中利用策略"""
    max_projects_per_billing = CONFIG.max_projects_per_billing
    unbound_count = len(unbound_projects)
    
    if not unbound_count or not active_billings:
        return []
    
    # 计算每个账单的当前状态
    billing_loads = []
    for billing in active_billings:
        current_load = current_usage.get(billing, 0)
        available_slots = max_projects_per_billing - current_load
        if available_slots > 0:  # 只考虑还有空位的账单
            billing_loads.append((billing, current_load, available_slots))
    
    if not billing_loads:
        logging.warning("所有账单都已达到最大项目数限制")
        return []
    
    # 集中利用策略：优先填满已有项目的账单
    billing_loads.sort(key=lambda x: (-x[1], -x[2]))
    
    # 制定分配计划
    allocation_plan = []
    remaining_projects = unbound_count
    
    logging.info("账单当前状态:")
    for billing, current_load, available_slots in billing_loads:
        billing_id = billing.split('/')[-1]
        logging.info(f"  {billing_id}: {current_load}个项目, {available_slots}个空位")
    
    for billing, current_load, available_slots in billing_loads:
        if remaining_projects <= 0:
            break
            
        allocate_count = min(available_slots, remaining_projects)
        if allocate_count > 0:
            allocation_plan.append((billing, allocate_count))
            remaining_projects -= allocate_count
            
            billing_id = billing.split('/')[-1]
            new_total = current_load + allocate_count
            logging.info(f"  计划: {billing_id} {current_load}→{new_total} (+{allocate_count}个项目)")
    
    if remaining_projects > 0:
        logging.warning(f"还有 {remaining_projects} 个项目无法分配，所有可用账单都已满")
    
    return allocation_plan

def redistribute_projects(
    unbound_projects: List[str],
    active_billings: List[str],
    current_usage: Dict[str, int],
    api_client: GoogleAPIClient,
    service_account_id: int,
    session: Session
) -> List[str]:
    """重新分配无账单项目 - 智能负载均衡"""
    if not unbound_projects or not active_billings:
        return []
    
    allocation_plan = get_billing_allocation_plan(unbound_projects, active_billings, current_usage)
    
    if not allocation_plan:
        logging.warning("无法制定有效的分配计划，所有账单可能都已满")
        return unbound_projects
    
    plan_info = ", ".join([f"{billing.split('/')[-1]}({count}个)" for billing, count in allocation_plan])
    logging.info(f"分配计划: {len(unbound_projects)} 个项目 → {plan_info}")
    
    # 执行分配
    successful_bindings = 0
    failed_bindings = 0
    failed_projects = []
    project_index = 0
    
    for target_billing, allocate_count in allocation_plan:
        successful_count = 0
        
        while successful_count < allocate_count and project_index < len(unbound_projects):
            project_id = unbound_projects[project_index]
            
            try:
                update_project_billing_info_v1(api_client, project_id, target_billing)
                log_operation(
                    operation_type='auto_bind',
                    service_account_id=service_account_id,
                    project_id=project_id,
                    billing_account_id=target_billing.split('/')[-1],
                    old_value='None',
                    new_value=target_billing,
                    status='success',
                    message=f"智能分配到账单 (负载均衡)",
                    session=session
                )
                successful_bindings += 1
                successful_count += 1
                logging.info(f"成功绑定项目 {project_id} 到账单 {target_billing}")
                
            except Exception as e:
                log_operation(
                    operation_type='auto_bind',
                    service_account_id=service_account_id,
                    project_id=project_id,
                    billing_account_id=target_billing.split('/')[-1],
                    old_value='None',
                    new_value=target_billing,
                    status='failed',
                    message=str(e),
                    session=session
                )
                failed_bindings += 1
                failed_projects.append(project_id)
                logging.error(f"绑定项目 {project_id} 到账单 {target_billing} 失败: {e}")
            
            project_index += 1
    
    # 处理剩余未分配的项目
    remaining_projects = unbound_projects[project_index:]
    if remaining_projects:
        logging.warning(f"还有 {len(remaining_projects)} 个项目未能分配: {remaining_projects}")
        failed_projects.extend(remaining_projects)
    
    logging.info(f"重新分配完成: 成功 {successful_bindings} 个, 失败 {failed_bindings} 个")
    
    return failed_projects

def process_account(app, gcp_account: Dict[str, str]) -> bool:
    """处理单个GCP服务账号 - 完全线程安全版本"""
    api_client = None
    
    try:
        with app.app_context():
            # 创建线程独立的数据库会话
            with create_db_session() as session:
                credentials_file = gcp_account['credentials_file']
                
                # 创建API客户端
                credentials = service_account.Credentials.from_service_account_file(
                    credentials_file,
                    scopes=['https://www.googleapis.com/auth/cloud-platform']
                )
                api_client = GoogleAPIClient(credentials, gcp_account['name'])
                
                # 获取服务账号邮箱
                service_account_email = get_service_account_email(credentials_file)
                
                # 查找或创建服务账号记录
                sa_obj = session.query(ServiceAccount).filter_by(name=gcp_account['name']).first()
                if not sa_obj:
                    sa_obj = ServiceAccount(
                        name=gcp_account['name'],
                        email=service_account_email,
                        credentials_file=credentials_file
                    )
                    session.add(sa_obj)
                    session.flush()  # 获取ID但不提交事务
                
                # 获取项目和账单信息
                projects = get_projects_v3(api_client)
                billing_accounts = get_billing_accounts_v1(api_client)
                
                # 处理账单账户信息
                billing_accounts_dict = {account['name']: account for account in billing_accounts}
                active_billing_accounts = [account['name'] for account in billing_accounts if account['open']]
                
                # 更新数据库中的账单账户信息
                for account in billing_accounts:
                    db_account = session.query(BillingAccount).filter_by(
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
                        session.add(db_account)
                    else:
                        db_account.display_name = account['displayName']
                        db_account.is_open = account['open']
                
                # 第一阶段：收集项目状态
                projects_billing_info = {}
                failed_projects = []
                unbound_projects = []
                
                logging.info(f"开始处理服务账号 {gcp_account['name']} 的 {len(projects)} 个项目")
                
                for project_id in projects:
                    billing_info = get_project_billing_info_v1(api_client, project_id)
                    if billing_info:
                        current_billing_account = billing_info.get('billingAccountName', 'None')
                        projects_billing_info[project_id] = current_billing_account
                        
                        if current_billing_account == 'None':
                            unbound_projects.append(project_id)
                        elif current_billing_account not in active_billing_accounts:
                            failed_projects.append((project_id, current_billing_account))
                            logging.info(f"发现失效账单项目: {project_id} -> {current_billing_account}")
                    else:
                        projects_billing_info[project_id] = 'None'
                        unbound_projects.append(project_id)
                
                # 第二阶段：批量解绑失效账单项目
                if failed_projects:
                    logging.info(f"开始解绑 {len(failed_projects)} 个失效账单项目")
                    
                    for project_id, old_billing in failed_projects:
                        try:
                            update_project_billing_info_v1(api_client, project_id, '')
                            unbound_projects.append(project_id)
                            projects_billing_info[project_id] = 'None'
                            
                            log_operation(
                                operation_type='unbind',
                                service_account_id=sa_obj.id,
                                project_id=project_id,
                                billing_account_id=old_billing.split('/')[-1],
                                old_value=old_billing,
                                new_value='None',
                                status='success',
                                message="失效账单自动解绑",
                                session=session
                            )
                            logging.info(f"成功解绑项目 {project_id} 的失效账单")
                            
                        except Exception as e:
                            log_operation(
                                operation_type='unbind',
                                service_account_id=sa_obj.id,
                                project_id=project_id,
                                billing_account_id=old_billing.split('/')[-1],
                                old_value=old_billing,
                                new_value='None',
                                status='failed',
                                message=str(e),
                                session=session
                            )
                            logging.error(f"解绑项目 {project_id} 失败: {e}")
                
                # 第三阶段：统一分配无账单项目
                if unbound_projects and active_billing_accounts and CONFIG.enable_auto_switch:
                    current_usage = get_current_billing_usage(projects_billing_info)
                    
                    logging.info(f"当前账单使用情况: {current_usage}")
                    logging.info(f"开始重新分配 {len(unbound_projects)} 个无账单项目")
                    
                    failed_redistribute_projects = redistribute_projects(
                        unbound_projects, 
                        active_billing_accounts, 
                        current_usage,
                        api_client, 
                        sa_obj.id,
                        session
                    )
                    
                    if failed_redistribute_projects:
                        logging.warning(f"有 {len(failed_redistribute_projects)} 个项目分配失败，将在下次运行时重试")
                    
                    # 重新获取项目账单信息
                    for project_id in unbound_projects:
                        try:
                            billing_info = get_project_billing_info_v1(api_client, project_id)
                            if billing_info:
                                projects_billing_info[project_id] = billing_info.get('billingAccountName', 'None')
                        except Exception as e:
                            logging.error(f"重新获取项目 {project_id} 账单信息失败: {e}")
                
                # 第四阶段：更新数据库记录
                used_billing_accounts = set()
                
                for project_id in projects:
                    current_billing_account = projects_billing_info.get(project_id, 'None')
                    
                    display_name = 'None'
                    if current_billing_account != 'None':
                        account_info_temp = billing_accounts_dict.get(current_billing_account, {})
                        display_name = account_info_temp.get('displayName', current_billing_account)
                        used_billing_accounts.add(current_billing_account)
                    
                    # 更新项目信息
                    db_project = session.query(Project).filter_by(
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
                        session.add(db_project)
                    else:
                        db_project.billing_account_id = current_billing_account.split('/')[-1] if current_billing_account != 'None' else None
                        db_project.billing_account_name = current_billing_account
                        db_project.billing_account_display_name = display_name
                
                # 更新账单使用状态
                for db_account in session.query(BillingAccount).filter_by(service_account_id=sa_obj.id).all():
                    db_account.is_used = db_account.name in used_billing_accounts
                
                # 事务会自动提交
                logging.info(f"成功处理服务账号 {gcp_account['name']}")
                return True
            
    except Exception as e:
        logging.error(f"处理服务账号 {gcp_account['name']} 时发生错误: {str(e)}", exc_info=True)
        return False
    finally:
        # 确保API客户端被正确关闭
        if api_client:
            api_client.close()

def update_project_status(app):
    """定期更新项目状态的后台任务 - 改进版超时处理"""
    consecutive_failures = 0
    max_consecutive_failures = 3
    
    while True:
        start_time = time.time()
        sleep_time = CONFIG.update_interval
        
        try:
            logging.info("开始执行定期账单检查和换绑任务")
            
            with app.app_context():
                gcp_accounts = app.config['GCP_ACCOUNTS']
                
                if not gcp_accounts:
                    logging.warning("没有配置GCP服务账号，跳过本次执行")
                    time.sleep(CONFIG.update_interval)
                    continue
                
                # 自适应线程数
                max_workers = min(CONFIG.max_workers, max(2, len(gcp_accounts)))
                
                # 改进的超时处理
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    future_to_account = {
                        executor.submit(process_account, app, account): account 
                        for account in gcp_accounts
                    }
                    
                    success_count = 0
                    failed_count = 0
                    
                    # 使用as_completed的超时参数，而不是future.result的超时
                    try:
                        for future in as_completed(future_to_account, timeout=CONFIG.task_timeout):
                            account = future_to_account[future]
                            try:
                                result = future.result()  # 不再设置timeout
                                if result:
                                    success_count += 1
                                else:
                                    failed_count += 1
                            except Exception as e:
                                failed_count += 1
                                logging.error(f"处理服务账号 {account['name']} 时发生异常: {e}")
                    
                    except TimeoutError:
                        logging.error(f"任务执行超时 ({CONFIG.task_timeout}s)，取消剩余任务")
                        # 取消所有未完成的任务
                        for future in future_to_account:
                            if not future.done():
                                future.cancel()
                        failed_count += len([f for f in future_to_account if not f.done()])
            
            execution_time = time.time() - start_time
            
            # 健康状态管理
            if failed_count == 0:
                consecutive_failures = 0
                logging.info(f"✅ 定期任务完成: 成功 {success_count} 个账号, 执行时间 {execution_time:.2f} 秒")
                log_metrics(success_count, failed_count, execution_time)
            else:
                consecutive_failures += 1
                logging.warning(f"⚠️  定期任务部分失败: 成功 {success_count} 个账号, 失败 {failed_count} 个账号, 连续失败 {consecutive_failures} 次")
                
                if consecutive_failures >= max_consecutive_failures:
                    extra_wait = min(300, consecutive_failures * 60)
                    logging.error(f"🚨 连续失败 {consecutive_failures} 次，额外等待 {extra_wait} 秒")
                    sleep_time += extra_wait
                    
                    if consecutive_failures >= 5:
                        send_alert_if_configured(f"GCP账单管理系统连续失败{consecutive_failures}次")
            
            # 计算下次执行的等待时间
            sleep_time = max(30, sleep_time - execution_time)
            logging.info(f"等待 {sleep_time:.2f} 秒后开始下一轮")
            
        except KeyboardInterrupt:
            logging.info("收到中断信号，正在停止后台任务...")
            break
        except Exception as e:
            consecutive_failures += 1
            logging.critical(f"🚨 后台任务发生严重错误 (连续失败 {consecutive_failures} 次): {e}", exc_info=True)
            sleep_time = min(600, CONFIG.update_interval * 2)
            logging.error(f"等待 {sleep_time} 秒后重试...")
        
        time.sleep(sleep_time)

def log_metrics(success_count: int, failed_count: int, execution_time: float):
    """记录监控指标"""
    metrics_info = {
        'timestamp': datetime.utcnow().isoformat(),
        'success_count': success_count,
        'failed_count': failed_count,
        'execution_time': execution_time,
        'total_accounts': success_count + failed_count,
        'config': {
            'max_workers': CONFIG.max_workers,
            'task_timeout': CONFIG.task_timeout,
            'max_qps_per_account': CONFIG.max_qps_per_account
        }
    }
    logging.info(f"METRICS: {json.dumps(metrics_info)}")

def send_alert_if_configured(message: str):
    """发送告警"""
    webhook_url = os.getenv('ALERT_WEBHOOK_URL')
    if webhook_url:
        try:
            # 这里可以实现具体的告警发送逻辑
            logging.info(f"发送告警: {message}")
        except Exception as e:
            logging.error(f"发送告警失败: {e}")

# ==================== 手动操作相关的函数 ====================

def remove_project_admin_rights(project_id: str, service_account_id: int) -> Tuple[bool, str]:
    """解除服务账号对项目的Admin权限 - 线程安全版本"""
    try:
        with create_db_session() as session:
            # 获取服务账号信息 - 使用SQLAlchemy 2.x兼容写法
            service_account_obj = session.get(ServiceAccount, service_account_id)
            if not service_account_obj:
                return False, "找不到指定的服务账号"
            
            # 获取项目信息
            project = session.query(Project).filter_by(
                project_id=project_id, 
                service_account_id=service_account_id
            ).first()
            
            if not project:
                return False, "找不到指定的项目"
            
            # 创建API客户端
            credentials = service_account.Credentials.from_service_account_file(
                service_account_obj.credentials_file,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )
            api_client = GoogleAPIClient(credentials, service_account_obj.name)
            
            try:
                # 调用API解除项目权限
                success = remove_project_admin_permission_v3(
                    api_client, 
                    project_id, 
                    service_account_obj.email
                )
                
                # 记录操作
                log_operation(
                    operation_type='remove_project_permission',
                    service_account_id=service_account_id,
                    project_id=project_id,
                    old_value="project.admin",
                    new_value="removed" if success else "failed",
                    status='success' if success else 'failed',
                    message=f"{'成功' if success else '失败'}解除项目Admin权限",
                    session=session
                )
                
                return success, "成功解除项目Admin权限" if success else "解除项目Admin权限失败"
                
            finally:
                api_client.close()
        
    except Exception as e:
        # 记录操作失败
        try:
            with create_db_session() as session:
                log_operation(
                    operation_type='remove_project_permission',
                    service_account_id=service_account_id,
                    project_id=project_id,
                    old_value="project.admin",
                    new_value="failed",
                    status='failed',
                    message=str(e),
                    session=session
                )
        except:
            pass
        
        return False, f"解除权限过程中发生错误: {str(e)}"

def remove_billing_admin_rights(billing_account_name: str, service_account_id: int) -> Tuple[bool, str]:
    """解除服务账号对账单的Billing Admin权限 - 线程安全版本"""
    try:
        with create_db_session() as session:
            # 获取服务账号信息 - 使用SQLAlchemy 2.x兼容写法
            service_account_obj = session.get(ServiceAccount, service_account_id)
            if not service_account_obj:
                return False, "找不到指定的服务账号"
            
            # 创建API客户端
            credentials = service_account.Credentials.from_service_account_file(
                service_account_obj.credentials_file,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )
            api_client = GoogleAPIClient(credentials, service_account_obj.name)
            
            try:
                # 解除权限
                success = remove_billing_admin_permission_v1(
                    api_client, 
                    billing_account_name, 
                    service_account_obj.email
                )
                
                # 记录操作
                log_operation(
                    operation_type='remove_permission',
                    service_account_id=service_account_id,
                    billing_account_id=billing_account_name.split('/')[-1],
                    old_value="billing.admin",
                    new_value="removed" if success else "failed",
                    status='success' if success else 'failed',
                    message=f"{'成功' if success else '失败'}解除Billing Admin权限",
                    session=session
                )
                
                return success, "成功解除Billing Admin权限" if success else "解除Billing Admin权限失败"
                
            finally:
                api_client.close()
        
    except Exception as e:
        # 记录操作失败
        try:
            with create_db_session() as session:
                log_operation(
                    operation_type='remove_permission',
                    service_account_id=service_account_id,
                    billing_account_id=billing_account_name.split('/')[-1] if billing_account_name else "unknown",
                    old_value="billing.admin",
                    new_value="failed",
                    status='failed',
                    message=str(e),
                    session=session
                )
        except:
            pass
        
        return False, f"解除权限过程中发生错误: {str(e)}"

def unbind_project_billing(project_id: str, service_account_id: int) -> Tuple[bool, str]:
    """解绑项目的账单信息 - 线程安全版本"""
    try:
        with create_db_session() as session:
            # 获取服务账号信息 - 使用SQLAlchemy 2.x兼容写法
            service_account_obj = session.get(ServiceAccount, service_account_id)
            if not service_account_obj:
                return False, "找不到指定的服务账号"
            
            # 获取项目信息
            project = session.query(Project).filter_by(
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
            
            # 创建API客户端
            credentials = service_account.Credentials.from_service_account_file(
                service_account_obj.credentials_file,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )
            api_client = GoogleAPIClient(credentials, service_account_obj.name)
            
            try:
                # 解绑账单
                update_project_billing_info_v1(api_client, project_id, '')
                
                # 更新项目信息
                project.billing_account_id = None
                project.billing_account_name = 'None'
                project.billing_account_display_name = 'None'
                
                # 记录操作
                log_operation(
                    operation_type='unbind',
                    service_account_id=service_account_id,
                    project_id=project_id,
                    billing_account_id=old_billing_account_id,
                    old_value=old_billing_account_name,
                    new_value='None',
                    status='success',
                    message="手动解绑项目账单",
                    session=session
                )
                
                return True, "成功解绑项目账单"
                
            except Exception as e:
                # 记录操作失败
                log_operation(
                    operation_type='unbind',
                    service_account_id=service_account_id,
                    project_id=project_id,
                    billing_account_id=old_billing_account_id,
                    old_value=old_billing_account_name,
                    new_value='None',
                    status='failed',
                    message=str(e),
                    session=session
                )
                
                return False, f"解绑项目账单失败: {str(e)}"
                
            finally:
                api_client.close()
    
    except Exception as e:
        logging.error(f"解绑项目账单过程中发生错误: {str(e)}")
        return False, f"解绑过程中发生错误: {str(e)}"

def delete_billing_account_record(billing_id: str, service_account_id: int) -> Tuple[bool, str]:
    """从数据库中删除账单账户记录 - 线程安全版本"""
    try:
        with create_db_session() as session:
            # 查找对应的账单记录
            billing_account = session.query(BillingAccount).filter_by(
                account_id=billing_id, 
                service_account_id=service_account_id
            ).first()
            
            if not billing_account:
                return False, "未找到对应的账单记录"
            
            # 检查是否有项目使用此账单
            projects_using_billing = session.query(Project).filter_by(
                billing_account_id=billing_id,
                service_account_id=service_account_id
            ).all()
            
            if projects_using_billing:
                return False, f"有 {len(projects_using_billing)} 个项目正在使用此账单，无法删除"
            
            # 记录操作
            log_operation(
                operation_type='delete_billing',
                service_account_id=service_account_id,
                billing_account_id=billing_id,
                status='success',
                message='从系统中删除账单记录',
                session=session
            )
            
            # 删除账单记录
            session.delete(billing_account)
            
            return True, "账单记录已成功删除"
        
    except Exception as e:
        logging.error(f"删除账单账户记录失败: {str(e)}")
        return False, str(e)
