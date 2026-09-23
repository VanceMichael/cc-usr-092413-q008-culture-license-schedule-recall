# 文化素材授权换版与课表撤回

服务管理文化素材授权、动作单元和年级课表，为期限检查与撤回处理提供 Flask 后端入口。

## 开发命令

- 安装依赖：`python3 -m pip install -r requirements.txt`
- 运行测试：`python3 -m pytest -q tests/test_health.py`
- 编译或构建检查：`python3 -m compileall -q app.py tests`
- 启动服务：`python3 app.py`

测试和构建只使用仓库内数据，不需要连接外部业务服务。
