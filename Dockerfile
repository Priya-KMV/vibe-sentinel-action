FROM python:3.12-slim

WORKDIR /github/workspace

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py /main.py

ENTRYPOINT ["python", "/main.py"]