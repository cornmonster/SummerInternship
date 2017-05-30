#!/bin/bash
num_file=0
num_dir=0
file_prefix="$3"

echo "Start creating subdir and files..."

while [ "$num_file" -lt "$2" ]
do
	pwgen 10000 1 > "garble_file_$file_prefix$num_file"
	num_file=$((num_file+1))
done

while [ "$num_dir" -lt "$1" ]
do
	mkdir "subdir_$file_prefix$num_dir"
	echo "---subdir_$file_prefix$num_dir created"
	cd "subdir_$file_prefix$num_dir"
	num_file=0
	while [ "$num_file" -lt "$2" ]
	do
		pwgen 10000 1 > "garble_file_$file_prefix$num_file"
		num_file=$((num_file+1))
	done


	num_dir=$((num_dir+1))
	cd ..
done

echo "Garble files created"