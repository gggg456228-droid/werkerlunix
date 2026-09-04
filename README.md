# Omega Universal Worker Linux Mirror

Публичный credential free Linux дистрибутив Omega Universal Worker.

Главный приватный source repository остаётся `gggg456228-droid/worker`. Этот репозиторий предназначен для установки на RunPod, VPS и другие Linux узлы без GitHub login, PAT и owner private key.

## Безопасность

На удалённый узел передаётся только `owner_public.pem`.

Никогда не помещай сюда:

* `owner_private.pem`
* GitHub PAT или GitHub credentials
* `config.json`
* `.runtime/`
* модели, jobs, outputs и cache

## Установка

```bash
git clone https://github.com/gggg456228-droid/werkerlunix.git omega-worker
cd omega-worker
# copy owner_public.pem into this directory
chmod +x install_linux.sh start_linux.sh
./install_linux.sh
```

По умолчанию worker слушает только `127.0.0.1:8765`. Для удалённого доступа используй SSH tunnel или приватную overlay сеть.

## Автообновление

Файл `.omega_public_update_url` указывает на ZIP текущей ветки этого публичного зеркала.

Каждый запуск через `start_linux.sh` сначала запускает `public_update.py`, который скачивает ZIP по HTTPS, сравнивает core файлы, создаёт backup изменяемых файлов в `.runtime/backups/`, обновляет только опубликованный core и затем запускает worker.

Для этого не требуются `.git`, GitHub login, GitHub PAT или доступ к приватному source repository.

Сохраняются локальные `config.json`, `.runtime`, `owner_public.pem` и данные в `~/omega-worker-data`.
