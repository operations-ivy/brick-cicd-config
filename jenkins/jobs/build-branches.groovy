// Build what pushed branches ask for: feature/* as dev-<commit>, release/* as
// the newTag Docker Hub doesn't have yet (bin/find-builds), through build-image.
// Jenkins' own image too: on every brick-cicd-config branch, the tag its
// jenkins/values.yaml names, and each commit is marked on GitHub with whether
// that image exists (bin/github-status), which main requires before a merge.
// cron H/2 * * * *
pipeline {
    agent none
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 3, unit: 'HOURS') }
    stages {
        stage('Find') {
            agent { label 'built-in' }
            steps {
                script {
                    def images = brickImages().collect { image, entry ->
                        "'${image}=${entry.repo}${entry.values ? ':' + entry.values : ''}'" }.join(' ')
                    here = pwd()
                    // Exit 4: a release's tag is taken; it's named above, the rest still builds.
                    def out = "${pwd()}/wanted"
                    // Last poll's statuses must not post again (a stale pending over a success).
                    sh "rm -f '${pwd()}/statuses'"
                    def status = sh(script: "/usr/share/brick-jenkins/bin/find-builds '${pwd()}/seen' " +
                                            "--statuses '${pwd()}/statuses' ${images} > '${out}'",
                                    returnStatus: true)
                    sh "/usr/share/brick-jenkins/bin/github-status '${pwd()}/status-queue' '${pwd()}/statuses'"
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
                        def entry = brickImages()[w[0]]
                        if (entry.values) {  // this commit's mark on GitHub: built or not
                            def state = run.result == 'SUCCESS' ? 'success' : 'failure'
                            node('built-in') {
                                writeFile file: "${here}/result", text: "${entry.repo} ${w[2]} ${w[0]} ${w[1]} ${state} ${run.absoluteUrl}\n"
                                sh "/usr/share/brick-jenkins/bin/github-status '${here}/status-queue' '${here}/result'"
                            }
                        }
                    }
                }
            }
        }
    }
}
