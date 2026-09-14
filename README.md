# Wiren Board Agent

[![Coverage Status](https://coveralls.io/repos/github/wirenboard/wb-cloud-agent/badge.svg)](https://coveralls.io/github/wirenboard/wb-cloud-agent)

Агент с использованием аппаратной подписи для получения обновлений с wirenboard.cloud.
[Подробная документация](https://wirenboard.com/wiki/Wiren_Board_Cloud)

## Как установить

Обновите список пакетов на контроллере и установите пакет

```
apt update && apt install wb-cloud-agent
```

## Как использовать

Для подключения контроллера через веб-интерфейс воспользутейсь [разделом настроек](https://wirenboard.com/wiki/Wiren_Board_Cloud#%D0%94%D0%BE%D0%B1%D0%B0%D0%B2%D0%BB%D0%B5%D0%BD%D0%B8%D0%B5_%D0%BA%D0%BE%D0%BD%D1%82%D1%80%D0%BE%D0%BB%D0%BB%D0%B5%D1%80%D0%B0_%D0%B2_%D0%BE%D0%B1%D0%BB%D0%B0%D0%BA%D0%BE).
Чтобы получить ссылку для подключения контроллера в консоли, выполните:
```
wb-cloud-agent
```
