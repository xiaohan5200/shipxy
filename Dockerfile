FROM mcr.microsoft.com/playwright/python:v1.51.0-noble

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY shipxy_server.py .
COPY shipxy_locator.py .
COPY shipxy_web.html .

# 暴露端口
EXPOSE 8765

# 启动（绑定 0.0.0.0 以允许外部访问）
CMD ["python", "shipxy_server.py", "--host", "0.0.0.0", "--port", "8765"]
