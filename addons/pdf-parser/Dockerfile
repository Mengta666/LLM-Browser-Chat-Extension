ARG CUDA_IMAGE=nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04
FROM ${CUDA_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
WORKDIR /opt/browser-agent-ocr
COPY addons/pdf-parser/requirements.txt addons/pdf-parser/requirements.txt
COPY deploy/pdf-parser/requirements-check.txt deploy/pdf-parser/requirements-check.txt
RUN python -m pip install --no-cache-dir paddlepaddle-gpu==3.2.1 \
      -i https://www.paddlepaddle.org.cn/packages/stable/cu126/ \
    && python -m pip install --no-cache-dir \
      -r addons/pdf-parser/requirements.txt -r deploy/pdf-parser/requirements-check.txt \
    && python -m pip check \
    && useradd --uid 10001 --create-home parser \
    && mkdir /models && chown 10001:10001 /models
COPY addons/pdf-parser/pdf_parser/ addons/pdf-parser/pdf_parser/
COPY deploy/pdf-parser/manage.py deploy/pdf-parser/check.py deploy/pdf-parser/
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "deploy/pdf-parser/manage.py"]
CMD ["parser", "--from-environment"]
