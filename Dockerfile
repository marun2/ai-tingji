# AI听记（企业中台 · 带后台的应用）。前端是原生 JS，不需要打包，镜像里不装 Node。
FROM python:3.12-slim
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATA_DIR=/app/data
WORKDIR /app
COPY requirements.txt .
# 先走阿里云内网 pip 源（服务器在阿里云，快），不通再走公网源
RUN pip install --default-timeout=20 --retries 3 -i http://mirrors.cloud.aliyuncs.com/pypi/simple/ \
        --trusted-host mirrors.cloud.aliyuncs.com -r requirements.txt \
 || pip install --default-timeout=20 --retries 3 -r requirements.txt
# OCR 依赖的 opencv 换成不带图形界面的版本（精简镜像里没有 libGL）
RUN (pip uninstall -y opencv-python >/dev/null 2>&1 || true) \
 && (pip install --default-timeout=20 --retries 3 -i http://mirrors.cloud.aliyuncs.com/pypi/simple/ --trusted-host mirrors.cloud.aliyuncs.com opencv-python-headless \
     || pip install --default-timeout=20 --retries 3 opencv-python-headless) \
 && python -c "import cv2, pypdfium2; from rapidocr_onnxruntime import RapidOCR"
COPY app ./app
# 上传的 Excel 原件、会议材料 PDF（/app/data/materials）在 /app/data（中台部署时挂成持久目录）；会议数据在中台共用的 PostgreSQL
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz',timeout=4)"
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
