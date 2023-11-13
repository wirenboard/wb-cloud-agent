# Wiren Board Agent 

Очень простой агент с использованием аппаратной подписи для получения обновлений с wirenboard.cloud

1. Периодически ринимает события с эндпоинта https://agent.wirenboard.cloud/api-agent/v1/events/
2. Обновляет файлы конфигурации и перезапускает необходимые службы (frpc.service или telegraf.service)
3. Подтверждает события по эндпоинту https://agent.wirenboard.cloud/api-agent/v1/events/{id}/confirm/ 
                                                                           
Актуальную версию можно скачать тут
                                                                                
```
wget https://d85a2ae2-5556-4eb0-94dd-c6bd6565c3a9.selstorage.ru/eeLi8pah2oozepai/wb_cloud_agent.deb
```


## Сборка через Docker

Пример сборки пакета представлен в Dockerfile. Сначала необходимо собрать сам образ, например:

```
docker build . -t agent-dist-builder
```

После чего нужно скопировать deb изнутри образа в систему, например, так:

```
id=$(docker create agent-dist-builder)                
docker cp $id:/src/deb_dist/python3-wb-cloud-agent_0.1.0-1_all.deb wb_cloud_agent.deb
docker rm -v $id
```

## Зависимости

Пакет измеет в зависимостях только python-requests
                            
```
apt install python3-requests
```

## Установка

```
dpkg -i wb_cloud_agent.deb
```

## Запуск

В консоли

```
wb-cloud-agent
```

В качестве службы нужно прописать конфиг сервиса

```
# /etc/systemd/system/wb-cloud-agent.service

[Unit]
Description=Wiren Board Cloud Agent
StartLimitIntervalSec=3600
StartLimitBurst=100

[Service]
ExecStart=wb-cloud-agent
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

И запустить сервис 

```systemctl start wb-cloud-agent.service```

## Конфигурирование

Для конфигурирования используются переменные окружения. 

WIRENBOARD_CLOUD_URL - url для запросов агента, значение по умолчанию 'http://localhost:7000/api-agent/v1/'

WIRENBOARD_FRP_CONFIG - файл для конфигурации туннеля (для перезаписи), значение по умолчанию '/root/soft/frp/frpc.conf'

WIRENBOARD_TELEGRAF_CONFIG - файл для конфигурации метрик (для перезаписи), значение по умолчанию '/root/soft/telegraf/telegraf.conf'

WIRENBOARD_ACTIVATION_LINK_CONFIG - файл для конфигурацией ссылки активации контроллера, значение по умолчанию '/root/soft/activation_link/activation_link.conf')

WIRENBOARD_REQUEST_PERIOD_SECONDS - частота опроса эндпоинта событий в секундах, значение по умолчанию '3'

## Конфигурация локального прокси

Для работы аппаратного ключа удобнее всего воспользоваться nginx, как описано тут https://wirenboard.com/wiki/CryptodevATECCx08_Auth

Например, можно прописать агент в `/etc/nginx/sites-enabled/agent` так:

```
server {
	listen   7000;
	server_name localhost;

    location / { 
        proxy_pass                 https://agent.wirenboard.cloud;
        proxy_ssl_name             agent.wirenboard.cloud;
        proxy_ssl_server_name      on; 
        proxy_ssl_certificate      /etc/ssl/certs/device_bundle_good.crt.pem;
        proxy_ssl_certificate_key  engine:ateccx08:ATECCx08:00:02:C0:00;
    }  
}
```

Где `device_bundle_good.crt.pem` - пофикшенный файл с сертификатом (см. по ссылке выше скрипт fix.sh)
                
## Конфигурация сервисов туннеля и метрик

Актуальный бинарник телеграфа можно скачать тут

```
wget https://d85a2ae2-5556-4eb0-94dd-c6bd6565c3a9.selstorage.ru/eeLi8pah2oozepai/telegraf
```

И установить его в качестве службы `telegraf.service`
   
```
# /etc/systemd/system/telegraf.service

[Unit]
Description=telegraf metric sender
StartLimitIntervalSec=3600
StartLimitBurst=100


[Service]
ExecStart=/root/soft/telegraf/telegraf --config /root/soft/telegraf/telegraf.conf
Restart=always
RestartSec=10


[Install]
WantedBy=multi-user.target
```

Актуальный бинарник frpc можно скачать тут

```
wget https://d85a2ae2-5556-4eb0-94dd-c6bd6565c3a9.selstorage.ru/eeLi8pah2oozepai/frpc
```

И установить его в качестве службы `frpc.service`
   
```
# /etc/systemd/system/frpc.service

[Unit]
Description=telegraf metric sender
StartLimitIntervalSec=3600
StartLimitBurst=100


[Service]
ExecStart=/root/soft/frp/frpc -c /root/soft/frp/frpc.conf
Restart=always
RestartSec=10


[Install]
WantedBy=multi-user.target
```
                                      
При этом пути к конфигам должны соответствовать тем, которые были указаны для агента.

