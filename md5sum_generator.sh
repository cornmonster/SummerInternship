#!/bin/bash
find ./ -type f -exec md5sum {} \; > checksums.md5
