FROM python:3.13.5-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x start.sh && addgroup --system app && adduser --system --ingroup app app && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["./start.sh"]
