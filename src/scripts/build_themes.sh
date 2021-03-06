#!/bin/bash
DIR_ROOT=$(pwd)
THEME_ROOT=${DIR_ROOT}/src/themes
cd $THEME_ROOT

for folder in $(find $THEME_ROOT -maxdepth 1 -mindepth 1 -type d -printf '%f\n')
do
        echo "Building theme: ${folder}"
        THEME_NAME=$( echo $folder | rev | cut -d'/' -f1 | rev )
        cd ${folder}
        rm -rf ./dist
        npm install
        npm start
        rm -rf ${DIR_ROOT}/http/${THEME_NAME}
        cp -r dist ${DIR_ROOT}/http/${THEME_NAME}
        cd ${THEME_ROOT}
done
