FROM python:3.11-slim

WORKDIR /app
COPY server.py /app/server.py
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8081
EXPOSE 8081
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=3).status==200 else 1)"
CMD ["python", "-I", "server.py"]
