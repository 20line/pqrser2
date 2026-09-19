<#
.SYNOPSIS
    Регистрирует задачу Планировщика заданий Windows для Avito Watcher Bot (п.12 ТЗ).

.DESCRIPTION
    Создаёт задачу с триггером «при входе в систему», которая запускает
    main.py в рабочем каталоге проекта. Настройки задачи включают
    автоматический перезапуск при падении процесса (встроенный watchdog
    Планировщика заданий — отдельный сторонний watchdog не требуется,
    см. п.12 ТЗ).

.EXAMPLE
    Запускать из PowerShell от имени администратора:
        powershell -ExecutionPolicy Bypass -File deploy\register_task.ps1

    Для удаления задачи:
        Unregister-ScheduledTask -TaskName "AvitoWatcherBot" -Confirm:$false
#>

param(
    [string]$TaskName = "AvitoWatcherBot",
    [string]$ProjectDir = (Resolve-Path "$PSScriptRoot\.."),
    [string]$PythonExe = "python"
)

Write-Host "Регистрирую задачу '$TaskName' для каталога '$ProjectDir'..."

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument "main.py" -WorkingDirectory $ProjectDir
$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Задача уже существует, обновляю..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Персональный бот слежения за Avito (Watcher Engine + Telegram Bot)" | Out-Null

Write-Host "Готово. Проверить статус: Get-ScheduledTask -TaskName $TaskName"
Write-Host "Запустить прямо сейчас: Start-ScheduledTask -TaskName $TaskName"
