// frontend/static/js/webrtc.js

class InterviewStreamer {
    constructor(sessionId, callbacks) {
        this.sessionId = sessionId;
        this.wsUrl = `ws://${window.location.host}/ws/interview/${sessionId}`;
        
        // Callbacks to update the UI from this class
        this.onConnect = callbacks.onConnect || function(){};
        this.onAIQuestion = callbacks.onAIQuestion || function(){};
        this.onInterimTranscript = callbacks.onInterimTranscript || function(){};
        this.onFinalTranscript = callbacks.onFinalTranscript || function(){};
        this.onInterviewComplete = callbacks.onInterviewComplete || function(){};
        this.onError = callbacks.onError || function(){};
        this.onSilenceDetect = callbacks.onSilenceDetect || function(){};

        this.ws = null;
        this.globalStream = null;
        this.mediaRecorder = null;
        this.isRecording = false;

        // Audio Context for Silence Detection
        this.audioContext = null;
        this.analyser = null;
        this.silenceDetectorId = null;
    }

    async requestMicrophone() {
        try {
            this.globalStream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
            });
            return this.globalStream;
        } catch (err) {
            this.onError("Microphone access denied. Please check your browser settings.");
            throw err;
        }
    }

    connect() {
        this.ws = new WebSocket(this.wsUrl);

        this.ws.onopen = () => this.onConnect();

        this.ws.onmessage = (event) => {
            const data = JSON.parse(event.data);

            if (data.type === "ai_question" || data.type === "interview_complete") {
                this.onAIQuestion(data);
                if (data.type === "interview_complete") {
                    this.onInterviewComplete(data);
                }
            } 
            else if (data.type === "interim_transcript") {
                this.onInterimTranscript(data.text);
            } 
            else if (data.type === "transcript_success") {
                this.onFinalTranscript(data.text);
            } 
            else if (data.type === "info" || data.type === "error") {
                this.onError(data.message);
                // Auto-retry recording after a network error
                setTimeout(() => this.startRecording(), 2000);
            }
        };

        this.ws.onerror = (err) => this.onError("Connection lost. Please refresh.");
    }

    startRecording() {
        if (this.isRecording || !this.globalStream || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        
        this.mediaRecorder = new MediaRecorder(this.globalStream, { mimeType: 'audio/webm;codecs=opus' });

        this.mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0 && this.ws.readyState === WebSocket.OPEN) {
                const reader = new FileReader();
                reader.readAsDataURL(e.data);
                reader.onloadend = () => {
                    const base64Audio = reader.result.split(',')[1];
                    this.ws.send(JSON.stringify({ type: "audio_chunk", audio: base64Audio }));
                };
            }
        };

        this.mediaRecorder.start(250);
        this.ws.send(JSON.stringify({ type: "start_recording" }));
        this.isRecording = true;

        this.startSilenceDetection();
    }

    stopRecording() {
        if (!this.isRecording) return;
        this.isRecording = false;

        if (this.mediaRecorder && this.mediaRecorder.state !== "inactive") {
            this.mediaRecorder.stop();
        }
        if (this.silenceDetectorId) {
            cancelAnimationFrame(this.silenceDetectorId);
        }

        this.ws.send(JSON.stringify({ type: "stop_recording" }));
    }

    startSilenceDetection() {
        if (!this.audioContext) {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            this.analyser = this.audioContext.createAnalyser();
            const microphone = this.audioContext.createMediaStreamSource(this.globalStream);
            microphone.connect(this.analyser);
        }

        const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
        let hasSpoken = false;
        let silenceStartTime = Date.now();

        const checkAudioLevel = () => {
            if (!this.isRecording) return;

            this.analyser.getByteFrequencyData(dataArray);
            let volume = dataArray.reduce((a, b) => a + b, 0) / dataArray.length;

            if (volume > 15) { 
                if (!hasSpoken) {
                    hasSpoken = true;
                    this.onSilenceDetect("listening"); // Trigger UI update
                }
                silenceStartTime = Date.now(); 
            } else {
                let silenceDuration = Date.now() - silenceStartTime;

                // Stop recording if silence exceeds thresholds
                if ((hasSpoken && silenceDuration > 3000) || (!hasSpoken && silenceDuration > 15000)) {
                    this.onSilenceDetect("processing");
                    this.stopRecording();
                    return;
                }
            }
            this.silenceDetectorId = requestAnimationFrame(checkAudioLevel);
        };
        checkAudioLevel();
    }
}

window.InterviewStreamer = InterviewStreamer;
