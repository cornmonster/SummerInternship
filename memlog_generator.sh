#!/bin/bash

while [ 1 ]
do
	cat /proc/meminfo | grep MemFree | cut -d' ' -f10 >> memlog
	sleep 10
done