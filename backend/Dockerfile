FROM python:3.11-slim
WORKDIR /app
# tzdata: zoneinfo is stdlib but python:3.11-slim ships no zone database, and
# hour_band/day_type must resolve Europe/London to agree with the dbt marts.
RUN pip install --no-cache-dir flask flask-cors duckdb tzdata
COPY . .
RUN mkdir -p /data
CMD ["python", "app.py"]
