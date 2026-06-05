@echo off
title App NFS-e Padrao Nacional
echo Instalando dependencias...
pip install -r requirements.txt
echo.
echo Abrindo o app...
streamlit run app_nfse_padrao_nacional.py
pause
