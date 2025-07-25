// static/js/main.js - 完整版

// 页面加载时执行
document.addEventListener('DOMContentLoaded', function() {
    initializeUI();
    setupEventListeners();
});

// 初始化UI组件
function initializeUI() {
    // 当确认模态框显示时，自动聚焦确认按钮
    const confirmModal = document.getElementById('confirmModal');
    if (confirmModal) {
        confirmModal.addEventListener('shown.bs.modal', function () {
            document.getElementById('confirmModalButton').focus();
        });
    }
    
    // 初始化所有工具提示组件
    initTooltips();
    
    // 配置axios默认设置
    configureAxios();
    
    // 添加加载动画的样式
    addLoadingStyles();
}

// 设置事件监听器
function setupEventListeners() {
    // 刷新数据按钮
    const refreshDataBtn = document.getElementById('refresh-data');
    if (refreshDataBtn) {
        refreshDataBtn.addEventListener('click', function() {
            showLoading();
            // 根据当前页面类型刷新不同数据
            if (window.location.pathname === '/') {
                loadSystemStatus();
                loadServiceAccounts().then(() => {
                    hideLoading();
                });
            } else if (window.location.pathname.startsWith('/accounts/')) {
                const accountId = window.location.pathname.split('/').pop();
                refreshAccountDetails(accountId).then(() => {
                    hideLoading();
                });
            }
        });
    }
    
    // 刷新账号列表按钮
    const refreshAccountsBtn = document.getElementById('refresh-accounts-btn');
    if (refreshAccountsBtn) {
        refreshAccountsBtn.addEventListener('click', function() {
            showLoading();
            loadServiceAccounts().then(() => {
                hideLoading();
            });
        });
    }
}

// 加载系统状态
function loadSystemStatus() {
    axios.get('/api/status')
        .then(function(response) {
            if (response.data.status === 'success') {
                const data = response.data.data;
                
                // 更新计数
                document.getElementById('service-account-count').textContent = data.counts.service_accounts;
                document.getElementById('project-count').textContent = data.counts.projects;
                document.getElementById('active-billing-count').textContent = data.counts.active_billing_accounts;
                document.getElementById('inactive-billing-count').textContent = data.counts.inactive_billing_accounts;
                
                // 更新最近操作
                const recentOperations = document.getElementById('recent-operations');
                if (recentOperations) {
                    recentOperations.innerHTML = '';
                    
                    data.recent_operations.forEach(function(op) {
                        const row = document.createElement('tr');
                        
                        // 设置样式
                        if (op.status === 'failed') {
                            row.classList.add('table-danger');
                        } else {
                            row.classList.add('table-success');
                        }
                        
                        row.innerHTML = `
                            <td>${formatOperationType(op.operation_type)}</td>
                            <td>${op.project_id || op.billing_account_id || '-'}</td>
                            <td>${op.status === 'success' ? '<span class="status-valid">成功</span>' : '<span class="status-invalid">失败</span>'}</td>
                            <td>${formatDate(op.created_at)}</td>
                        `;
                        
                        recentOperations.appendChild(row);
                    });
                }
                
                // 更新账单状态图表
                updateBillingChart(data.counts.active_billing_accounts, data.counts.inactive_billing_accounts);
            }
        })
        .catch(function(error) {
            console.error('加载系统状态失败:', error);
        });
}

// 更新账单状态图表 - 使用黑金配色
function updateBillingChart(active, inactive) {
    const ctx = document.getElementById('billing-chart');
    if (!ctx) return;
    
    const context = ctx.getContext('2d');
    
    // 检查是否已有图表实例，如果有则销毁
    if (window.billingChart) {
        window.billingChart.destroy();
    }
    
    // 创建新图表
    window.billingChart = new Chart(context, {
        type: 'doughnut',
        data: {
            labels: ['活跃账单', '失效账单'],
            datasets: [{
                data: [active, inactive],
                backgroundColor: [
                    'rgba(40, 167, 69, 0.8)',
                    'rgba(220, 53, 69, 0.8)'
                ],
                borderColor: [
                    'rgba(40, 167, 69, 1)',
                    'rgba(220, 53, 69, 1)'
                ],
                borderWidth: 1
            }]
        },
        options: {
            responsive: true,
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        color: '#333',
                        font: {
                            weight: '500'
                        }
                    }
                }
            }
        }
    });
}

// 加载服务账号列表
function loadServiceAccounts() {
    return axios.get('/api/service-accounts')
        .then(function(response) {
            if (response.data.status === 'success') {
                const accounts = response.data.data;
                const accountsList = document.getElementById('service-accounts-list');
                
                if (!accountsList) return;
                
                accountsList.innerHTML = '';
                
                accounts.forEach(function(account) {
                    const row = document.createElement('tr');
                    
                    row.innerHTML = `
                        <td>${account.name}</td>
                        <td>${account.email}</td>
                        <td>${account.project_count}</td>
                        <td><span class="status-valid">${account.active_billing_count}</span></td>
                        <td><span class="status-invalid">${account.inactive_billing_count}</span></td>
                        <td>
                            <a href="/accounts/${account.id}" class="btn btn-primary btn-sm">
                                <i class="bi bi-eye me-1"></i>查看详情
                            </a>
                        </td>
                    `;
                    
                    accountsList.appendChild(row);
                });
            }
        })
        .catch(function(error) {
            console.error('加载服务账号列表失败:', error);
        });
}

// 加载活跃账单
function loadActiveBillings() {
    axios.get('/api/billing-accounts?is_open=true')
        .then(function(response) {
            if (response.data.status === 'success') {
                const billings = response.data.data;
                const billingsList = document.getElementById('active-billings-list');
                
                if (!billingsList) return;
                
                billingsList.innerHTML = '';
                
                billings.forEach(function(billing) {
                    const row = document.createElement('tr');
                    
                    row.innerHTML = `
                        <td>${billing.account_id}</td>
                        <td>${billing.display_name || billing.name}</td>
                        <td>${billing.is_used ? '<span class="status-valid">使用中</span>' : '<span class="status-invalid">未使用</span>'}</td>
                        <td>${formatDate(billing.updated_at)}</td>
                    `;
                    
                    billingsList.appendChild(row);
                });
            }
        })
        .catch(function(error) {
            console.error('加载活跃账单失败:', error);
        });
}

// 加载失效账单
function loadInactiveBillings() {
    axios.get('/api/billing-accounts?is_open=false')
        .then(function(response) {
            if (response.data.status === 'success') {
                const billings = response.data.data;
                const billingsList = document.getElementById('inactive-billings-list');
                
                if (!billingsList) return;
                
                billingsList.innerHTML = '';
                
                billings.forEach(function(billing) {
                    const row = document.createElement('tr');
                    
                    row.innerHTML = `
                        <td>${billing.account_id}</td>
                        <td>${billing.display_name || billing.name}</td>
                        <td>${billing.is_used ? '<span class="status-valid">使用中</span>' : '<span class="status-invalid">未使用</span>'}</td>
                        <td>${formatDate(billing.updated_at)}</td>
                        <td>
                            <button class="btn btn-sm btn-warning" onclick="confirmRemovePermission('${billing.account_id}', ${billing.service_account_id})">
                                解除权限
                            </button>
                        </td>
                    `;
                    
                    billingsList.appendChild(row);
                });
            }
        })
        .catch(function(error) {
            console.error('加载失效账单失败:', error);
        });
}

// 初始化Bootstrap工具提示
function initTooltips() {
    const tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });
}

// 配置Axios
function configureAxios() {
    if (typeof axios !== 'undefined') {
        // 添加请求拦截器
        axios.interceptors.request.use(function(config) {
            // 在请求发送前处理
            showLoading();
            return config;
        }, function(error) {
            // 处理请求错误
            hideLoading();
            return Promise.reject(error);
        });
        
        // 添加响应拦截器
        axios.interceptors.response.use(function(response) {
            // 处理响应数据
            hideLoading();
            return response;
        }, function(error) {
            // 处理响应错误
            hideLoading();
            showErrorNotification('API请求错误: ' + (error.response?.data?.message || error.message));
            console.error('API请求错误:', error);
            return Promise.reject(error);
        });
    }
}

// 显示加载中动画
function showLoading() {
    let loader = document.getElementById('global-loader');
    if (!loader) {
        loader = document.createElement('div');
        loader.id = 'global-loader';
        loader.innerHTML = `
            <div class="spinner"></div>
            <p>加载中...</p>
        `;
        document.body.appendChild(loader);
    }
    loader.style.display = 'flex';
}

// 隐藏加载中动画
function hideLoading() {
    const loader = document.getElementById('global-loader');
    if (loader) {
        loader.style.display = 'none';
    }
}

// 添加加载动画样式
function addLoadingStyles() {
    if (!document.getElementById('loading-styles')) {
        const style = document.createElement('style');
        style.id = 'loading-styles';
        style.textContent = `
            #global-loader {
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background-color: rgba(255, 255, 255, 0.7);
                display: none;
                justify-content: center;
                align-items: center;
                flex-direction: column;
                z-index: 9999;
            }
            #global-loader .spinner {
                width: 40px;
                height: 40px;
                border: 4px solid rgba(13, 110, 253, 0.2);
                border-radius: 50%;
                border-top-color: #0d6efd;
                animation: spin 1s ease-in-out infinite;
            }
            #global-loader p {
                margin-top: 10px;
                color: #0d6efd;
                font-weight: 500;
            }
            @keyframes spin {
                to { transform: rotate(360deg); }
            }
            .fade-in {
                animation: fadeIn 0.3s ease-in-out;
            }
            @keyframes fadeIn {
                from { opacity: 0; }
                to { opacity: 1; }
            }
        `;
        document.head.appendChild(style);
    }
}

// 显示错误通知
function showErrorNotification(message) {
    if (!document.getElementById('notification-container')) {
        const container = document.createElement('div');
        container.id = 'notification-container';
        container.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 9999;
        `;
        document.body.appendChild(container);
    }
    
    const notification = document.createElement('div');
    notification.className = 'notification error fade-in';
    notification.style.cssText = `
        background-color: #f8d7da;
        color: #842029;
        border-left: 4px solid #dc3545;
        padding: 12px 20px;
        margin-bottom: 10px;
        border-radius: 4px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
        display: flex;
        align-items: center;
        max-width: 400px;
    `;
    
    notification.innerHTML = `
        <i class="bi bi-exclamation-triangle-fill me-2"></i>
        <span>${message}</span>
        <button type="button" class="btn-close ms-auto" aria-label="关闭"></button>
    `;
    
    document.getElementById('notification-container').appendChild(notification);
    
    // 添加关闭按钮事件
    notification.querySelector('.btn-close').addEventListener('click', function() {
        notification.remove();
    });
    
    // 自动关闭
    setTimeout(() => {
        notification.classList.add('fade-out');
        setTimeout(() => {
            notification.remove();
        }, 300);
    }, 5000);
}

// 刷新账号详情页所有数据
function refreshAccountDetails(accountId) {
    return Promise.all([
        loadAccountDetails(),
        loadProjects(),
        loadInactiveBillings(),
        loadActiveBillings(),
        loadOperations()
    ]);
}

// 格式化操作类型
function formatOperationType(type) {
    const types = {
        'update': '更新账单',
        'unbind': '解绑账单',
        'remove_permission': '移除账单权限',
        'remove_project_permission': '移除项目权限',
        'delete_project': '删除项目',
        'delete_billing': '删除账单'
    };
    
    return types[type] || type;
}

// 格式化日期
function formatDate(dateString) {
    if (!dateString) return '未知';
    
    const date = new Date(dateString);
    return isNaN(date.getTime()) ? '日期无效' : date.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
    });
}

// 创建状态标签
function createStatusBadge(status, text) {
    const badgeClass = status ? 'status-badge-success' : 'status-badge-danger';
    return `<span class="status-badge ${badgeClass}">${text}</span>`;
}