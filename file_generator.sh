#!/bin/bash
# ./file_generator <num of files you want> <directory>

idx=0
cd $2
while [ $idx -lt $1 ]
do
	pwgen 10000 1 > "testfile_$idx"
	idx=$((idx + 1))
done
