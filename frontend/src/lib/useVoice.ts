/**
 * Talking and listening, from the browser.
 *
 * The backend has had speech since Phase 7 -- POST /voice/transcribe takes
 * audio bytes and returns text, POST /voice/speak takes text and returns a
 * WAV. Until now the only way to reach either was curl, which is a strange
 * way to talk to an assistant.
 *
 * WHY MediaRecorder AND NOT THE WEB SPEECH API.
 *
 * Browsers ship a SpeechRecognition API that would do this in ten lines.
 * In Chrome it works by sending your audio to Google's servers. This
 * project runs Whisper locally, on purpose -- CLAUDE.md picks
 * faster-whisper so speech never leaves the machine. Using the browser API
 * would quietly undo that, and the code would look simpler for it.
 *
 * So: MediaRecorder captures the microphone, the bytes go to JARVIS's own
 * endpoint, and Whisper transcribes them on this computer.
 *
 * WHAT A TRANSCRIPT DOES AND DOES NOT DO.
 *
 * It fills the composer rather than sending. Whisper mishears things --
 * names, technical words, anything said over a fan -- and a wrong sentence
 * sent automatically is a wrong sentence acted on. One keystroke to confirm
 * is a small price, and the box is focused so Enter is all it takes.
 */

import { useCallback, useRef, useState } from "react";

export type VoiceState = "idle" | "recording" | "transcribing" | "speaking";

/** The formats worth trying, best first. Browsers differ on what they can record. */
const PREFERRED_TYPES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/mp4",
];

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  return PREFERRED_TYPES.find((type) => MediaRecorder.isTypeSupported(type));
}

export function useVoice() {
  const [state, setState] = useState<VoiceState>("idle");
  const [error, setError] = useState<string | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  /** Release the microphone. Without this the browser keeps showing "recording". */
  const releaseMic = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  const start = useCallback(async () => {
    setError(null);

    const mimeType = pickMimeType();
    if (!mimeType) {
      setError("This browser cannot record audio.");
      return;
    }

    try {
      // Prompts for permission the first time. In the Electron shell the
      // main process has to approve it too -- see desktop/main.js.
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;

      const recorder = new MediaRecorder(stream, { mimeType });
      chunksRef.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.start();

      recorderRef.current = recorder;
      setState("recording");
    } catch (exc) {
      releaseMic();
      setState("idle");
      // The common causes are a denied permission and no microphone at all,
      // and the browser's own message names which.
      setError(`Could not use the microphone: ${(exc as Error).message}`);
    }
  }, [releaseMic]);

  /**
   * Stop recording and transcribe. Resolves to the text, or "" if there was
   * no speech -- which is a normal outcome, not a failure: pressing the
   * button and saying nothing is a thing people do.
   */
  const stopAndTranscribe = useCallback(async (): Promise<string> => {
    const recorder = recorderRef.current;
    if (!recorder) return "";

    const finished = new Promise<Blob>((resolve) => {
      recorder.onstop = () =>
        resolve(new Blob(chunksRef.current, { type: recorder.mimeType }));
    });

    recorder.stop();
    const audio = await finished;
    releaseMic();
    recorderRef.current = null;

    setState("transcribing");
    try {
      // Raw bytes, not multipart -- the endpoint reads request.body()
      // directly, which avoids a dependency for a single-user local API.
      const response = await fetch("/voice/transcribe", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: audio,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? body.message ?? `HTTP ${response.status}`);
      }
      const result = await response.json();
      return (result.text ?? "").trim();
    } catch (exc) {
      setError(`Could not transcribe that: ${(exc as Error).message}`);
      return "";
    } finally {
      setState("idle");
    }
  }, [releaseMic]);

  const cancel = useCallback(() => {
    const recorder = recorderRef.current;
    if (recorder) {
      // Detached first, so the transcribe promise never fires for a
      // recording the user abandoned.
      recorder.onstop = null;
      recorder.stop();
    }
    recorderRef.current = null;
    chunksRef.current = [];
    releaseMic();
    setState("idle");
  }, [releaseMic]);

  /** Say something out loud, through Piper on this machine. */
  const speak = useCallback(async (text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;

    // Anything already playing is stopped first, so two replies arriving
    // close together do not talk over each other.
    audioRef.current?.pause();

    try {
      setState("speaking");
      const response = await fetch("/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // The endpoint caps text at 5000 characters, and a spoken reply
        // that long would be unbearable anyway.
        body: JSON.stringify({ text: trimmed.slice(0, 5000) }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audioRef.current = audio;

      // Released when playback ends, otherwise every reply leaks a blob for
      // as long as the window stays open.
      audio.onended = () => URL.revokeObjectURL(url);

      await audio.play();
    } catch (exc) {
      setError(`Could not speak that: ${(exc as Error).message}`);
    } finally {
      setState("idle");
    }
  }, []);

  const stopSpeaking = useCallback(() => {
    audioRef.current?.pause();
    audioRef.current = null;
    setState("idle");
  }, []);

  return {
    state,
    error,
    clearError: () => setError(null),
    start,
    stopAndTranscribe,
    cancel,
    speak,
    stopSpeaking,
    supported: typeof navigator !== "undefined" && !!navigator.mediaDevices,
  };
}
