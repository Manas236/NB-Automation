@echo off
:: Run this ONCE as Administrator (right-click -> Run as administrator).
:: 1) removes the rule that BLOCKS python.exe on the private network
:: 2) allows inbound TCP 5000 for the Newsband dashboard on private/domain networks
netsh advfirewall firewall delete rule name="python.exe" dir=in program="C:\users\admin\appdata\local\python\pythoncore-3.14-64\python.exe"
netsh advfirewall firewall add rule name="Newsband Post Maker (port 5000)" dir=in action=allow protocol=TCP localport=5000 profile=private,domain
echo.
echo Done. Other PCs can now open http://192.168.1.45:5000
pause
