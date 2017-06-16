#!/bin/bash
target_dir=$1
target_file=$2
find $1 -type f -exec md5sum {} \; > $2
