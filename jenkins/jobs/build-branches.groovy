// Build what pushed branches ask for: feature/* as dev-<commit>, release/* as
// the newTag Docker Hub doesn't have yet (bin/find-builds), through build-image.
// cron H/2 * * * *
pipeline {
    agent none
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 3, unit: 'HOURS') }
    stages {
        stage('Find') {
            agent { label 'built-in' }
            steps {
                script {
                    def images = brickImages().collect { image, entry -> "'${image}=${entry.repo}'" }.join(' ')
                    // Exit 4: a release's tag is taken; it's named above, the rest still builds.
                    def out = "${pwd()}/wanted"
                    def status = sh(script: "/usr/share/brick-jenkins/bin/find-builds '${pwd()}/seen' ${images} > '${out}'",
                                    returnStatus: true)
                    if (status == 4) {
                        unstable('A release branch pins a tag built from another commit')
                    } else if (status != 0) {
                        error("find-builds exited ${status}")
                    }
                    wanted = readFile(out).readLines().collect { it.split(' ') }
                }
            }
        }
        // No agent: this only waits on build-image, which has its own pod.
        stage('Build') {
            when { expression { wanted } }
            steps {
                script {
                    for (w in wanted) {
                        def run = build(job: 'build-image', propagate: false, parameters: [
                            string(name: 'IMAGE', value: w[0]),
                            string(name: 'TAG', value: w[1]),
                            string(name: 'REF', value: w[2]),
                        ])
                        if (run.result != 'SUCCESS') {
                            unstable("${w[0]}:${w[1]} didn't build: ${run.absoluteUrl}")
                        }
                    }
                }
            }
        }
    }
}
