#!/bin/bash
num_file=0
num_dir=0

while [ "$num_dir" -lt "$1" ]
do
	mkdir "subdir$num_dir"
	cd "subdir$num_dir"

	while [ "$num_file" -lt "$2" ]
	do
		pwgen 10000 1 > "garble_file_$num_file"
		num_file=$((num_file+1))
	done

	num_dir=$((num_dir+1))
	cd ..
done

echo "+OK Garble files created."