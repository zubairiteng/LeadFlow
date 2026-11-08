FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV LEADGEN_INSTANCE=/data PORT=5000
VOLUME /data
EXPOSE 5000
CMD ["python", "run.py"]
