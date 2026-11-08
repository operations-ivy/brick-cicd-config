// Take a brick out of service before it's powered down, and put it back. down: refuse while it's busy (apt, a game or a deploy on it, a Jenkins build on the node), open a maintenance window, stop what resumes on its own, cordon and drain a k3s node, then power it off. up: wait for it to answer, uncordon, end the window, run smoke. DRY_RUN (on by default) runs the checks and prints the plan only. brick2000 (Jenkins, every volume) and brick420 (the control plane) are refused, with the steps to do by hand.
// choice NODE=brick666,brick9000,brick1982 The brick to power down (or bring back)
// choice ACTION=down,up down: take it out of service; up: put it back once it's plugged in
// choice DURATION=4h,1h,12h,24h,indefinite The maintenance window (down only; up ends it)
// param SHUTDOWN=true Power it off at the end of down (untick to only drain and stop)
// param DRY_RUN=true Print the plan, change nothing
pipeline {
    agent { label 'built-in' }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 30, unit: 'MINUTES') }
    stages {
        stage('Power') {
            steps {
                script {
                    currentBuild.displayName = "#${currentBuild.number} ${params.NODE}: ${params.ACTION}${params.DRY_RUN ? ' (dry run)' : ''}"
                }
                sh '/usr/share/brick-jenkins/bin/power-down'
            }
        }
    }
}
