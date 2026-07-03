FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir flask flask-cors
COPY . .
RUN mkdir -p /data
CMD ["python", "app.py"]
