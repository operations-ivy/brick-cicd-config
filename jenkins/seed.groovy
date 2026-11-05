// Job DSL: one pipeline job per jobs/<name>.groovy, run by configuration as
// code at start-up (values.yaml). The image carries this file, the jobs and
// their scripts under /usr/share/brick-jenkins.
//
// A job file starts with comment lines:
//   // What the job does, in a sentence or two (becomes the description).
//   // param NAME=default  Help text  (true/false defaults make a checkbox)
//   // choice NAME=a,b,c  Help text   (a dropdown; the first is the default)
//   // choice NAME=@list  Help text   (a dropdown read from $JENKINS_HOME/choices/<list>,
//                                     one per line, kept up to date by a job that
//                                     re-runs this seed when it changes)
//   // cron H */2 * * *               (optional schedule; several cron lines
//                                     are joined, so one can be TZ=Area/City)
// then a declarative pipeline.
//
// Every job can be started from the LAN with a webhook, parameters in the
// query string:
//   curl -X POST 'https://jenkins.brick.nozdormu.cloud/generic-webhook-trigger/invoke?token=<name>-<secret>&NAME=value'
// where <secret> is the webhook-secret key of the brick-jenkins Secret (the
// same value as ~/.config/brick-jenkins/secrets/webhook_secret on brick9000).

def root = '/usr/share/brick-jenkins'
// The shared library (lib/*.groovy, e.g. plasma), put ahead of every job's
// pipeline: the jobs can call its functions as their own.
def library = new File("${root}/lib").listFiles().findAll { it.name.endsWith('.groovy') }.sort { it.name }
        .collect { it.text }.join('\n')
def secret = new File('/run/secrets/additional/webhook-secret').text.trim()
def home = System.getenv('JENKINS_HOME') ?: '/var/jenkins_home'

// A choice line's options: listed, or @list from $JENKINS_HOME/choices/<list>.
def options = { String spec ->
    if (!spec.startsWith('@')) {
        return spec.split(',').collect { it.trim() }.findAll { it }
    }
    def f = new File("${home}/choices/${spec.substring(1)}")
    def got = f.exists() ? f.readLines().collect { it.trim() }.findAll { it } : []
    return got ?: ['(not listed yet: run maintenance-refresh)']
}

new File("${root}/jobs").listFiles().findAll { it.name.endsWith('.groovy') }.sort { it.name }.each { file ->
    def name = file.name - '.groovy'
    def text = file.text
    def header = text.readLines().takeWhile { it.startsWith('//') }.collect { it.substring(2).trim() }
    def params = header.findAll { it.startsWith('param ') || it.startsWith('choice ') }.collect { line ->
        def m = line =~ /^(param|choice) (\w+)=(\S*)\s*(.*)$/
        assert m.matches(): "${file.name}: bad param line: ${line}"
        [name: m.group(2), value: m.group(3), help: m.group(4), choices: m.group(1) == 'choice' ? options(m.group(3)) : null]
    }
    def schedule = header.findAll { it.startsWith('cron ') }.collect { it.substring(5) }.join('\n') ?: null
    def about = header.findAll { !it.startsWith('param ') && !it.startsWith('choice ') && !it.startsWith('cron ') }.join('\n')

    pipelineJob(name) {
        description("${about}\n\nWebhook: POST https://jenkins.brick.nozdormu.cloud/generic-webhook-trigger/invoke?token=${name}-<webhook secret>" +
                    (params ? params.collect { "&${it.name}=…" }.join('') : ''))
        logRotator { numToKeep(30) }
        if (params) {
            parameters {
                params.each { p ->
                    if (p.choices != null) {
                        choiceParam(p.name, p.choices, p.help)
                    } else if (p.value in ['true', 'false']) {
                        booleanParam(p.name, p.value == 'true', p.help)
                    } else {
                        stringParam(p.name, p.value, p.help)
                    }
                }
            }
        }
        properties {
            pipelineTriggers {
                triggers {
                    genericTrigger {
                        // Query-string values fill the job's parameters of the same name.
                        genericRequestVariables {
                            params.each { p ->
                                genericRequestVariable {
                                    key(p.name)
                                    regexpFilter('')
                                }
                            }
                        }
                        token("${name}-${secret}")
                        tokenCredentialId('')
                        causeString('Started by webhook')
                        printContributedVariables(false)
                        printPostContent(false)
                        silentResponse(false)
                        shouldNotFlatten(false)
                        regexpFilterText('')
                        regexpFilterExpression('')
                    }
                    if (schedule) {
                        cron {
                            spec(schedule)
                        }
                    }
                }
            }
        }
        definition {
            cps {
                script(library + '\n' + text)
                sandbox(true)
            }
        }
    }
}
