// Build an app image from a commit of its repo, reproducibly, in a throwaway
// rootless BuildKit pod on brick2000, and push it to Docker Hub. Refuses a tag
// Docker Hub already has (tags are never reused). See lib/build.groovy.
// param IMAGE=whitepatrick/wigle-sync Image to build (its repo and Dockerfile come from lib/build.groovy)
// param REF=main Full commit hash, tag or branch to build
// param TAG= Tag to push, e.g. 0.1.8
// param PUSH=true Push to Docker Hub (otherwise only build and print the digest)
pipeline {
    agent { kubernetes { yaml buildkitPod() } }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 60, unit: 'MINUTES') }
    stages {
        stage('Check') {
            steps {
                script {
                    // Named from the start: brick-status shows it on a running build.
                    currentBuild.displayName = "#${currentBuild.number} ${params.IMAGE.split('/')[1]}:${params.TAG}"
                    checkBuildArgs(params.IMAGE, params.REF, params.TAG ?: null)
                    if (!params.TAG) {
                        error('build-image: TAG is required')
                    }
                    container('tools') {
                        // Exit 3: Docker Hub doesn't have the tag.
                        def status = sh(script: "/usr/share/brick-jenkins/bin/hub-image-info '${params.IMAGE}' '${params.TAG}'",
                                        returnStatus: true)
                        if (status != 3) {
                            error("build-image: ${params.IMAGE}:${params.TAG} is already on Docker Hub (or Hub can't be read); tags are never reused")
                        }
                    }
                }
            }
        }
        stage('Build') {
            steps {
                script {
                    def got = checkoutForBuild(params.IMAGE, params.REF, 'src')
                    def revision = got[0]
                    def epoch = got[1]
                    def digest = buildImage(params.IMAGE, params.TAG, 'src', revision, epoch, params.PUSH)
                    echo "build-image: ${params.IMAGE}:${params.TAG} = ${digest} (from ${revision})"
                }
            }
        }
    }
}
