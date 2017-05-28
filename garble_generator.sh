#!/bin/bash
num_file=0

cd "$2"

while [ "$num_file" -lt "$1" ]
do
	pwgen 10000 1 > "garble_file_$num_file"
	num_file=$((num_file+1))
done

cd ..
echo "+OK Garble files created."