# Поисковый стек

CRW предоставляет REST API поиска и чтения. SearXNG получает публичную выдачу;
Lightpanda и Chromium выполняют JavaScript, когда обычной загрузки недостаточно.
Chromium запускается Playwright и подключается к CRW через CDP. Пайплайн не зависит
от интерфейса или CLI какого-либо AI-редактора.

Проверен запуск на Linux x86_64 с CRW 0.35.1, SearXNG на коммите
`f4822b3fc46726bb259d14c6332b702c76b98f82`, Gunicorn 26.2.0,
Node 24 и Playwright 1.64.0. Конфигурация и запуск входят в эту папку.

## Установка на Linux

Команды выполняются из корня репозитория. Нужны Git, curl, Node 20+ и uv.
Бинарники CRW для других платформ есть в [релизе 0.35.1](https://github.com/fastcrw/crw/releases/tag/v0.35.1).

```sh
mkdir -p .cache/tools
curl -fL https://github.com/fastcrw/crw/releases/download/v0.35.1/crw-server-linux-x64.tar.gz \
  -o .cache/tools/crw.tar.gz
printf '%s  %s\n' \
  ff50a23f336f7658697025480c24682ea4a8029bb098533795839890749a036d \
  .cache/tools/crw.tar.gz | sha256sum -c -
tar -xzf .cache/tools/crw.tar.gz -C .cache/tools

git clone https://github.com/searxng/searxng.git .cache/tools/searxng
git -C .cache/tools/searxng checkout f4822b3fc46726bb259d14c6332b702c76b98f82
uv venv .cache/tools/search-venv
uv pip install --python .cache/tools/search-venv/bin/python \
  -r .cache/tools/searxng/requirements.txt gunicorn==26.2.0

npm ci --prefix stack
./stack/node_modules/.bin/playwright install chromium
```

На свежем Linux Chromium нужны системные библиотеки. Команда
`./stack/node_modules/.bin/playwright install-deps chromium` устанавливает пакеты ОС
и требует прав администратора. Запускайте браузер от обычного пользователя: sandbox включён.

[Lightpanda](https://github.com/lightpanda-io/browser#install) распространяется
nightly-бинарниками. Сохраните выбранный бинарник в `.cache/tools/lightpanda`,
сделайте исполняемым и запишите вывод `lightpanda version`. Можно подключить уже
работающий локальный CDP-сервер на порту 9222. Без Lightpanda CRW использует Chromium.

## Запуск

```sh
uv run python stack/certificates.py
uv run python stack/run.py \
  --crw-bin .cache/tools/crw-server \
  --searxng .cache/tools/searxng \
  --searx-python .cache/tools/search-venv/bin/python \
  --lightpanda-bin .cache/tools/lightpanda \
  --ca-file .cache/russian-ca.pem
```

Из другого терминала:

```sh
curl -sS http://127.0.0.1:3080/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"Дейта Кью официальный сайт","limit":5,"lang":"ru"}'
```

CRW слушает только `127.0.0.1:3080`. Журналы и PID — `.cache/stack`.
Ctrl+C или SIGTERM завершают только запущенные этим скриптом процессы.
Если порт занят, скрипт откажется запускаться. Порты можно изменить параметрами
`--crw-port`, `--searx-port`, `--chrome-port`; при этом измените `CRW_URL` клиента.

Если стек стоит на другой машине, доступ можно дать SSH-туннелем:

```sh
ssh -N -L 13080:127.0.0.1:3080 user@server
export CRW_URL=http://127.0.0.1:13080
```

Публичный доступ к CRW и CDP не требуется. Сетевые адреса проверяются клиентом,
обход учитывает robots.txt. Для промышленного сервиса дополнительно нужна сетевая
изоляция браузеров; эти настройки не заявлены как полноценная защита от SSRF.

## Необязательный резерв поиска

`search_gateway.py` принимает SearXNG-совместимый запрос и при пустой выдаче
обращается к Tavily или Brave Search REST. По умолчанию он выключен. Для включения
нужен свой ключ и `--remote-search tavily` либо `--remote-search brave` при запуске.
Ключи передаются через `TAVILY_API_KEY` или `BRAVE_SEARCH_API_KEY`.

Этот резерв проверен моками, живых запросов к этим API не было: ключей нет.
Итоговые проверки выполнены без него. Бесплатный лимит и отсутствие автоматических
платных списаний нужно проверить в своём аккаунте до включения.
