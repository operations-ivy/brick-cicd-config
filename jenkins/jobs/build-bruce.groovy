// Build Bruce firmware (github.com/BruceDevices/firmware) for an ESP32 board in a
// throwaway pod, and keep the images as this build's artifacts: the full image
// (flash at 0x0) and the app alone (keeps settings), with SHA256SUMS and
// BUILD-INFO.txt (flash commands). Started by hand; flash from the laptop.
// The default, INVERSION_OFF on CYD-2432S028, is for a CYD whose panel shows white
// for black under the stock build.
// param VERSION= Bruce release tag, e.g. 1.17 (empty: the latest release)
// param BOARD=CYD-2432S028 Bruce's PlatformIO environment (its boards/*/*.ini), e.g. CYD-2USB
// param INVERSION_OFF=true Build with -DTFT_INVERSION_ON: Bruce turns the panel's colour inversion off at boot instead of forcing it on
// param EXTRA_FLAGS= More build flags, space-separated: -DNAME or -DNAME=value
pipeline {
    agent {
        kubernetes {
            defaultContainer 'build'
            // build: PlatformIO and its ESP32 toolchain (downloaded each run, about 1 GB).
            // tools: this Jenkins image, for jenkins/bin/build-bruce.
            // No service-account token (values.yaml): the build can't touch the cluster.
            yaml '''
apiVersion: v1
kind: Pod
spec:
  containers:
    - name: build
      image: python:3.13-bookworm@sha256:cc55e4ded9c95f6ce2133b3d096db43f214d280c2a8af65d83e246c8fa7d168c
      command: [sleep]
      args: [infinity]
      resources:
        requests: {cpu: "1", memory: 1Gi}
        limits: {cpu: "4", memory: 3Gi}
    - name: tools
      image: docker.io/whitepatrick/brick-jenkins:@BRICK_JENKINS_TAG@
      command: [sleep]
      args: [infinity]
      resources:
        requests: {cpu: 50m, memory: 64Mi}
        limits: {cpu: 500m, memory: 256Mi}
'''
        }
    }
    options { disableConcurrentBuilds(); timestamps(); timeout(time: 90, unit: 'MINUTES') }
    stages {
        stage('Install') {
            steps {
                script {
                    // build-bruce checks the parameters; this name is only for the board.
                    currentBuild.displayName = "#${currentBuild.number} Bruce ${params.VERSION ?: 'latest'} ${params.BOARD}" +
                            (params.INVERSION_OFF ? ' invoff' : '')
                    plasma('install') {
                        container('tools') { sh 'cp /usr/share/brick-jenkins/bin/build-bruce .' }
                        sh 'pip install --no-cache-dir --quiet platformio==6.2.0'
                    }
                }
            }
        }
        stage('Build') {
            // The parameters reach build-bruce as environment variables, not on its command line.
            steps { script { plasma('build') { sh 'rm -rf out work && python3 build-bruce out' } } }
        }
    }
    post {
        success { archiveArtifacts artifacts: 'out/*', fingerprint: true }
    }
}
