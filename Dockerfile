FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV CF_DATA_DIR=/app/data
EXPOSE 5100

CMD ["python", "main.py"]
