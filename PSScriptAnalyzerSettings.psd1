# PSScriptAnalyzer configuration for MAST_supervision's PowerShell (install/).
#
# MAST_provisioning's doctrine, kept to the rules that apply here: the default rule set,
# the version pinned in CI (1.25.0, as MAST_provisioning pins it), and each exclusion
# named with its reason.
@{
    IncludeDefaultRules = $true

    ExcludeRules        = @(
        # The installer is run by hand and by MAST_provisioning's mast-service provider, whose
        # log captures the console: its output is the operator's record, not pipeline objects.
        'PSAvoidUsingWriteHost',

        # nssm.exe and sc.exe are documented and invoked with positional arguments.
        'PSAvoidUsingPositionalParameters'
    )
}
