FROM python:3.12-alpine

WORKDIR /app
COPY . /app

RUN apk add --no-cache tzdata

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py
COPY webui.py /app/webui.py
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

EXPOSE 7575

ENTRYPOINT ["/app/entrypoint.sh"]
