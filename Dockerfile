# 1. 파이썬 3.12 슬림 버전 사용 (가볍고 빠름)
FROM python:3.12-slim

# [추가된 핵심 로직] 한글 깨짐 방지를 위한 언어팩(Locale) 설치
RUN apt-get update && apt-get install -y locales && \
    localedef -f UTF-8 -i ko_KR ko_KR.UTF-8
ENV LANG=ko_KR.UTF-8
ENV LC_ALL=ko_KR.UTF-8

# 2. 한국 시간대(KST) 설정 (봇의 시간/로그가 한국 시간에 맞게 찍히도록)
ENV TZ=Asia/Seoul
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 3. 작업 폴더 설정
WORKDIR /app

# 4. 파이썬 환경 변수 (로그가 버퍼링 없이 즉시 출력되도록 설정)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 5. 필수 패키지 설치
COPY requirements.txt .
# pip 자체를 최신으로 업데이트하고, 경고 무시 옵션을 추가하여 설치
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# 6. 소스 코드 전체 복사
COPY . .

# 7. FastAPI 포트 개방
EXPOSE 8000

# 8. 앱 실행 명령어 (단일 루프 통합본 실행)
CMD ["python", "main.py"]