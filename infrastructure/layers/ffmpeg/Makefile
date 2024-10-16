build-FfmpegLambdaPackage:
	curl https://www.johnvansickle.com/ffmpeg/old-releases/ffmpeg-6.0.1-amd64-static.tar.xz --output ffmpeg.tar.xz
	tar xvf ffmpeg.tar.xz
	mkdir -p "$(ARTIFACTS_DIR)/bin"
	cp ffmpeg-6.0.1-amd64-static/ffmpeg "$(ARTIFACTS_DIR)/bin/"
