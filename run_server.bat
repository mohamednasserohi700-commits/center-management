@echo off
chcp 1256 > nul

title سنتر الدروس الخصوصية - Web Server

echo.
echo  ============================================
echo   سنتر الدروس الخصوصية
echo   Web Server - Port 5000
echo  ============================================
echo.

python --version > nul 2>&1
if errorlevel 1 (
    echo  [خطأ] Python غير مثبت
    pause
    exit /b 1
)

echo  جاري تشغيل السيرفر...
echo  افتح المتصفح على: http://localhost:5000
echo  لإيقاف السيرفر اضغط Ctrl+C
echo.

start "" http://localhost:5000
python server.py

pause
