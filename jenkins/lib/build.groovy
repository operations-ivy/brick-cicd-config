// Reproducible image builds, shared by build-image and verify-images so the
// verify rebuilds exactly the way the build built.
//
// A build runs in a throwaway pod on brick2000 (buildkitPod): rootless
// BuildKit, pinned by digest (another version can lay out layers
// differently and change the digest), the agent's own container (git), and
// this Jenkins image as "tools" (hub-image-info). SOURCE_DATE_EPOCH is the
// commit's time and file timestamps are rewritten to it, so a commit always
// builds the same digest (proven on brick2000, 2026-10-13: two --no-cache
// builds of wigle-sync 9af5ee8 gave the same sha256:02f6ac72...).

// Every image built from these repos: name -> repo and Dockerfile.
def brickImages() {
    return [
        'whitepatrick/wigle-sync':    [repo: 'wigle-sync', dockerfile: 'Dockerfile'],
        'whitepatrick/wigle-console': [repo: 'wigle-sync', dockerfile: 'Dockerfile.web'],
        'whitepatrick/joke-reader':   [repo: 'chucks-wisdom', dockerfile: 'Dockerfile.web'],
        'whitepatrick/joke-importer': [repo: 'chucks-wisdom', dockerfile: 'Dockerfile.db'],
        'whitepatrick/overwatch':     [repo: 'overwatch', dockerfile: 'Dockerfile'],
    ]
}

// The pod. Rootless BuildKit needs seccomp and AppArmor unconfined and no
// process sandbox (it has no user namespaces to make one); it still runs as
// uid 1000, with no service-account token (values.yaml).
def buildkitPod() {
    return '''
apiVersion: v1
kind: Pod
spec:
  containers:
    - name: buildkit
      image: moby/buildkit:v0.32.2-rootless@sha256:504731e577c20559c00f968f33219f30115e70be29ab96728d1d06e963fc494b
      command: [sleep]
      args: [infinity]
      env:
        - {name: BUILDKITD_FLAGS, value: --oci-worker-no-process-sandbox}
      securityContext:
        runAsUser: 1000
        runAsGroup: 1000
        seccompProfile: {type: Unconfined}
        appArmorProfile: {type: Unconfined}
      resources:
        requests: {cpu: 500m, memory: 512Mi}
        limits: {cpu: "2", memory: 2Gi}
    - name: tools
      image: docker.io/whitepatrick/brick-jenkins:2.580.1-9
      command: [sleep]
      args: [infinity]
      resources:
        requests: {cpu: 50m, memory: 64Mi}
        limits: {cpu: 500m, memory: 256Mi}
'''
}

// Refuse anything that could do more than name an image, a tag or a commit:
// these come from webhooks and end up in shell commands.
def checkBuildArgs(String image, String ref, String tag) {
    if (!brickImages().containsKey(image)) {
        error("build: unknown image '${image}' (one of ${brickImages().keySet().join(', ')})")
    }
    if (!(ref ==~ /[0-9a-f]{40}|[A-Za-z0-9._\/-]{1,100}/)) {
        error("build: bad ref '${ref}'")
    }
    if (tag != null && !(tag ==~ /[A-Za-z0-9][A-Za-z0-9._-]{0,127}/)) {
        error("build: bad tag '${tag}'")
    }
}

// Clone the image's repo at ref into dir; returns [revision, epoch].
def checkoutForBuild(String image, String ref, String dir) {
    def repo = brickImages()[image].repo
    def out = ''
    plasma('checkout') {
        container('jnlp') {
            sh "rm -rf '${dir}' && git init -q '${dir}' && git -C '${dir}' fetch -q --depth 1 " +
               "https://github.com/operations-ivy/${repo} '${ref}' && git -C '${dir}' checkout -q FETCH_HEAD"
            out = sh(script: "git -C '${dir}' log -1 --format='%H %ct'", returnStdout: true).trim()
        }
    }
    def parts = out.split(' ')
    return [parts[0], parts[1]]
}

// Build image:tag from dir (a checkout at revision, committed at epoch) and
// return the image's manifest digest. push: also push it to Docker Hub
// (the dockerhub credential). noCache: build every layer from scratch.
def buildImage(String image, String tag, String dir, String revision, String epoch,
               boolean push = false, boolean noCache = false) {
    def entry = brickImages()[image]
    def opts = "--frontend dockerfile.v0 --local context='${dir}' --local dockerfile='${dir}' " +
               "--opt filename='${entry.dockerfile}' --opt build-arg:SOURCE_DATE_EPOCH=${epoch} " +
               "--opt label:org.opencontainers.image.revision=${revision} " +
               "--opt label:org.opencontainers.image.source=https://github.com/operations-ivy/${entry.repo} " +
               (noCache ? '--no-cache ' : '') +
               "--metadata-file '${dir}.meta.json'"
    def output = "type=image,name=docker.io/${image}:${tag},rewrite-timestamp=true"
    container('buildkit') {
        plasma(noCache ? 'verify' : 'build') {
            sh "buildctl-daemonless.sh build ${opts} --output ${output},push=false"
        }
        if (push) {
            plasma('push') {
                withCredentials([usernamePassword(credentialsId: 'dockerhub', usernameVariable: 'HUB_USER',
                                                  passwordVariable: 'HUB_TOKEN')]) {
                    // The same build again, from cache, now pushed.
                    sh '''mkdir -p "$HOME/.docker" && umask 077 && printf '{"auths":{"https://index.docker.io/v1/":{"auth":"%s"}}}' \
                          "$(printf '%s:%s' "$HUB_USER" "$HUB_TOKEN" | base64 | tr -d '\\n')" > "$HOME/.docker/config.json"'''
                    try {
                        sh "buildctl-daemonless.sh build ${opts} --output ${output},push=true"
                    } finally {
                        sh 'rm -f "$HOME/.docker/config.json"'
                    }
                }
            }
        }
        return sh(script: "sed -n 's/.*\"containerimage.digest\": *\"\\(sha256:[0-9a-f]*\\)\".*/\\1/p' '${dir}.meta.json'",
                  returnStdout: true).trim()
    }
}
