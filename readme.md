# Docker Compose

| Команда                                      | Что делает                                                              | Когда использовать                                                                                                  | Режим работы                  |
|----------------------------------------------|-------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------|-------------------------------|
| `docker compose up`                          | Запускает контейнеры из уже существующих образов (без пересборки)       | Ничего не менял в коде/Dockerfile, просто поднять или перезапустить сервисы                                        | Foreground (логи в терминале) |
| `docker compose up fe-articles`              | Запускает только контейнер fe-articles | Ничего не менял в коде/Dockerfile, просто поднять или перезапустить сервисы                                        | Foreground (логи в терминале) |
| `docker compose up --build`                  | **Пересобирает** все образы → запускает контейнеры                      | Изменил код, Dockerfile, requirements.txt и т.д.                                                                   | Foreground                    |
| `docker compose up -d`                       | Запускает контейнеры **в фоне** (detached)                              | Хочешь запустить и отойти от терминала                                                                              | Background                    |
| `docker compose up -d --build`               | **Пересобирает образы + запускает в фоне** — САМАЯ ЧАСТАЯ КОМАНДА       | В 99% случаев: внёс любые изменения → одна команда всё пересобирает и поднимает                                     | Background                    |
| `docker compose up --build --force-recreate` | Пересобирает + **полностью пересоздаёт контейнеры** (удаляет старые)    | Глюки с томами, кэшем, сетью — нужно «с чистого листа»                                                                     | Foreground (или с `-d`)       |

## Ежедневные команды

```bash
# 1. Основная команда — после любых изменений
docker compose up -d --build

# 2. Просто перезапустить без пересборки (например изменился код или env)
docker compose up -d

# 3. Остановить всё
docker compose down
# с очисткой томов (если нужно):
docker compose down -v
```

# Руководство по запуску проекта (с нуля)

## 1. Поиск сервера для ботов

**Рекомендуемые провайдеры:** IONOS, Hetzner, Contabo, Kamatera *(мой выбор)*

**Параметры сервера:**
- **Локация:** Amsterdam или Frankfurt (минимальный пинг к Telegram)
- **ОС:** Ubuntu 24.04 LTS
- **CPU/RAM:** от 1–2 vCPU и 2 GB RAM
- **Диск:** 20–30 GB NVMe

---

## 2. Вход на сервер
IP сервера и пароль взять в аккаунте сервера

```bash
ssh root@65.109.12.34
# Enter your password
```

---

## 3. Проверка сервера и базовых настроек

### 1. User & System Info
```bash
whoami
pwd
hostnamectl | grep "Operating System"
```

### 2. RAM & Disk Overview
```bash
free -h
df -h
```

### 3. OS Version
```bash
cat /etc/os-release | grep PRETTY_NAME
```

### 4. Python & Pip
```bash
python3 --version
pip3 --version
```

---

## 4. Настройка сервера

### 1. Устанавливаем зависимости
```bash
apt update && apt install -y python3-pip python3-venv git
```

**Проверяем:**
```bash
python3 -V
pip3 --version
git --version
nginx -v
```

### 2. Создаем директорию для проекта
```bash
mkdir -p /opt/python-parser && cd /opt/python-parser
```

### 3. Клонируем проект
```bash
git clone https://github.com/[ АДРЕС ПРОЕКТА ].git .
```

**Проверяем наличие файлов проекта на сервере:**
```bash
ls -la
```

---

## 5. Настройка проекта (на примере number-challenge)

Заходим:
```bash
cd /opt/python-parser/services/number-challenge
```

### 1. Гененируем .env файл путем клонирования
```bash
cp .env.example .env
```

### 2. Редактируем `.env`
```bash
nano .env
# Insert your token and chat_id
# Ctrl+O → Enter → Ctrl+X
```

### 3. Создаем папку data
```bash
mkdir -p /data
```

---

## 6. Сборка и запуск Docker

 Идем в корень проекта:
```bash
cd /opt/python-parser
```

Собираем/запускаем контейнер:
```bash
docker compose up -d --build number-challenge
```

---

## 7. Открываем порт для UI проекта

Для данного проекта порт **5001**:
```bash
ufw allow 5001/tcp
```

---

## 8. Проверка работы UI


```
http://83.229.87.135:5001/
```

# Закинуть изменения по проекту

```bash
ssh root@YOUR_SERVER_IP
cd /opt/python-parser
git status
git pull origin main
docker compose up -d --build SERVISE_NAME
```
