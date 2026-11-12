#requires -RunAsAdministrator
#requires -Version 5.1
<#
.SYNOPSIS
    Register mast-service, the NSSM service that starts and watches the MAST supervisor.

.DESCRIPTION
    Idempotent: installs the service when it is absent, then sets every parameter on
    every run, so a re-run converges a changed or stale registration. Never starts it:
    on a unit, MAST_provisioning's mast-services-finalize starts it at the end of a run.

    Run as MAST_provisioning's mast-service provider runs it, from
    <Top>\supervision\install\, with <Top> the clone root holding .venv and supervision.
    What it registers is what that provider's verify expects (MAST_provisioning#262):
    nssm-hosted, LocalSystem, automatic start, <Top>\.venv\Scripts\python.exe
    -m supervision.service from <Top>.

    -Uninstall stops and removes the service, for use by hand. Provisioning removes it
    with its own Remove-MastService, which works on a checkout that predates this file.
#>
[CmdletBinding()]
param(
    [switch]${Uninstall}
)

Set-StrictMode -Version Latest
${ErrorActionPreference} = 'Stop'

${SERVICE_NAME}    = 'mast-service'
${DISPLAY_NAME}    = 'MAST supervisor service'
${DESCRIPTION}     = 'Starts and watches the MAST supervisor in the interactive session (MAST_supervision).'
${NSSM_EXE}        = 'C:\Program Files\nssm\nssm.exe'
${VENV_PYTHON}     = '.venv\Scripts\python.exe'
${SERVICE_MODULE}  = 'supervision.service'
${SERVICE_ACCOUNT} = 'LocalSystem'
${START_MODE}      = 'SERVICE_AUTO_START'
${LOG_DIR}         = 'C:\MAST\logs\mast-service'
${STDOUT_LOG}      = 'nssm-stdout.log'
${STDERR_LOG}      = 'nssm-stderr.log'
${ROTATE_BYTES}    = 10485760
${ENABLED}         = 1
${MS_PER_SECOND}   = 1000

${APP_RESTART_DELAY_MS}     = 5000
${FAILURE_RESET_SECONDS}    = 86400
${FAILURE_RESTART_DELAY_MS} = 5000
${FAILURE_RESTARTS}         = 3

# The stop. NSSM sends Ctrl+C, waits STOP_CONSOLE_TIMEOUT_MS, then terminates service.py
# alone: the supervisor dies with the job service.py holds, and the supervised programs,
# spawned outside that job, survive (design section 4). WM_CLOSE and WM_QUIT are skipped,
# since service.py has no window and no message loop.
#
# The timeout is the supervisor's whole teardown: a probe already in flight, the app's
# shutdown wait, and a terminate-then-kill grace pair for each torn-down program
# (probes.py, supervisor.py, managed.py; tests/test_install_mast_service.py keeps them equal).
${PROBE_IN_FLIGHT_SECONDS}    = 70
${APP_SHUTDOWN_WAIT_SECONDS}  = 200
${PROGRAM_STOP_GRACE_SECONDS} = 10
${GRACES_PER_PROGRAM}         = 2
${TORN_DOWN_PROGRAMS}         = 4
${STOP_MARGIN_SECONDS}        = 60
${TEARDOWN_BUDGET_SECONDS}    = ${PROBE_IN_FLIGHT_SECONDS} + ${APP_SHUTDOWN_WAIT_SECONDS} + ${GRACES_PER_PROGRAM} * ${PROGRAM_STOP_GRACE_SECONDS} * ${TORN_DOWN_PROGRAMS} + ${STOP_MARGIN_SECONDS}
${STOP_CONSOLE_TIMEOUT_MS}    = ${TEARDOWN_BUDGET_SECONDS} * ${MS_PER_SECOND}
${STOP_METHOD_SKIP}           = 6
${KILL_PROCESS_TREE}          = 0

function Invoke-Tool {
    # Run a native tool and throw with its output on a non-zero exit. Under
    # ErrorActionPreference Stop, PowerShell 5.1 turns a native tool's stderr into a
    # terminating NativeCommandError before the exit code can be read, so the call is
    # isolated; nssm writes UTF-16, whose NULs are dropped from the message.
    param(
        [Parameter(Mandatory)][string]${Path},
        [Parameter(Mandatory)][string[]]${Arguments}
    )
    ${previous} = ${ErrorActionPreference}
    try {
        ${ErrorActionPreference} = 'Continue'
        ${output} = ((& ${Path} @Arguments 2>&1 | Out-String) -replace "`0", '').Trim()
        ${code} = ${LASTEXITCODE}
    }
    finally {
        ${ErrorActionPreference} = ${previous}
    }
    if (${code} -ne 0) {
        throw ("{0} {1} exited {2}: {3}" -f (Split-Path -Leaf ${Path}), (${Arguments} -join ' '), ${code}, ${output})
    }
}

function Test-NssmHosted {
    param([Parameter(Mandatory)][string]${Name})
    ${service} = Get-CimInstance -ClassName Win32_Service -Filter ("Name='{0}'" -f ${Name})
    return ($null -ne ${service}) -and (${service}.PathName -match 'nssm\.exe')
}

${existing} = Get-Service -Name ${SERVICE_NAME} -ErrorAction SilentlyContinue

if (${Uninstall}) {
    if ($null -eq ${existing}) {
        Write-Host ("{0}: not registered; nothing to remove" -f ${SERVICE_NAME})
        exit 0
    }
    if (${existing}.Status -ne 'Stopped') {
        Write-Host ("{0}: stopping (the supervisor tears its programs down first)" -f ${SERVICE_NAME})
        Stop-Service -Name ${SERVICE_NAME} -Force
    }
    Invoke-Tool -Path ${NSSM_EXE} -Arguments @('remove', ${SERVICE_NAME}, 'confirm')
    Write-Host ("{0}: removed" -f ${SERVICE_NAME})
    exit 0
}

if (-not (Test-Path -LiteralPath ${NSSM_EXE})) {
    Write-Warning ("{0}: nssm.exe not found at {1}; MAST_provisioning's nssm provider installs it" -f ${SERVICE_NAME}, ${NSSM_EXE})
    exit 1
}

${top} = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
${python} = Join-Path ${top} ${VENV_PYTHON}
if (-not (Test-Path -LiteralPath ${python})) {
    Write-Warning ("{0}: no interpreter at {1}; this script must run from <Top>\supervision\install" -f ${SERVICE_NAME}, ${python})
    exit 1
}

if ($null -eq ${existing}) {
    Write-Host ("{0}: installing" -f ${SERVICE_NAME})
    Invoke-Tool -Path ${NSSM_EXE} -Arguments @('install', ${SERVICE_NAME}, ${python})
}
elseif (-not (Test-NssmHosted -Name ${SERVICE_NAME})) {
    Write-Warning ("{0}: a service of this name is registered and nssm does not host it; remove it by hand first" -f ${SERVICE_NAME})
    exit 1
}

${null} = New-Item -ItemType Directory -Force -Path ${LOG_DIR}

${parameters} = [ordered]@{
    'Application'          = ${python}
    'AppParameters'        = ('-m {0}' -f ${SERVICE_MODULE})
    'AppDirectory'         = ${top}
    'ObjectName'           = ${SERVICE_ACCOUNT}
    'Start'                = ${START_MODE}
    'DisplayName'          = ${DISPLAY_NAME}
    'Description'          = ${DESCRIPTION}
    'AppStdout'            = (Join-Path ${LOG_DIR} ${STDOUT_LOG})
    'AppStderr'            = (Join-Path ${LOG_DIR} ${STDERR_LOG})
    'AppRotateFiles'       = ${ENABLED}
    'AppRotateOnline'      = ${ENABLED}
    'AppRotateBytes'       = ${ROTATE_BYTES}
    'AppRestartDelay'      = ${APP_RESTART_DELAY_MS}
    'AppStopMethodSkip'    = ${STOP_METHOD_SKIP}
    'AppStopMethodConsole' = ${STOP_CONSOLE_TIMEOUT_MS}
    'AppKillProcessTree'   = ${KILL_PROCESS_TREE}
}
foreach (${name} in ${parameters}.Keys) {
    Invoke-Tool -Path ${NSSM_EXE} -Arguments @('set', ${SERVICE_NAME}, ${name}, [string]${parameters}[${name}])
}

# NSSM restarts the application itself; these are for nssm.exe dying.
${actions} = (@(1..${FAILURE_RESTARTS} | ForEach-Object { 'restart/{0}' -f ${FAILURE_RESTART_DELAY_MS} }) -join '/')
Invoke-Tool -Path 'sc.exe' -Arguments @('failure', ${SERVICE_NAME}, 'reset=', [string]${FAILURE_RESET_SECONDS}, 'actions=', ${actions})

Write-Host ("{0}: registered ({1}, {2}, {3} -m {4} from {5}); not started" -f ${SERVICE_NAME}, ${SERVICE_ACCOUNT}, ${START_MODE}, ${python}, ${SERVICE_MODULE}, ${top})
exit 0
