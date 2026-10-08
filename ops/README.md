# ops/ — обслуживание сервера

Версионируемые копии того, что реально стоит на `fruntend-stage`
(`135.181.254.225`). Зачем они здесь, какие пороги и почему приняты такие
решения — в разделе «Обслуживание сервера» в корневом `readme.md`.

| Файл | Куда ставится на сервере |
|---|---|
| `server-health-alert.sh` | `/usr/local/bin/server-health-alert.sh` (755) |
| `cron.d/server-health-alert` | `/etc/cron.d/server-health-alert` (644, root:root) |
| `cron.d/docker-builder-prune` | `/etc/cron.d/docker-builder-prune` (644, root:root) |

Деплой этих файлов **не автоматизирован**: GitHub Actions выкатывает только
контейнеры, а эти скрипты живут на хосте и правятся редко. Установка вручную:

```bash
scp ops/server-health-alert.sh fruntend-stage:/usr/local/bin/
ssh fruntend-stage chmod 755 /usr/local/bin/server-health-alert.sh

scp ops/cron.d/server-health-alert ops/cron.d/docker-builder-prune fruntend-stage:/etc/cron.d/
ssh fruntend-stage 'chmod 644 /etc/cron.d/server-health-alert /etc/cron.d/docker-builder-prune'
```

Имена файлов в `/etc/cron.d/` не должны содержать точек — иначе cron их молча
игнорирует.

Проверка после установки:

```bash
ssh fruntend-stage /usr/local/bin/server-health-alert.sh   # печатает строку состояния
ssh fruntend-stage 'systemctl is-active cron'
```
