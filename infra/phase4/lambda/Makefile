REPOSITORY_ROOT := ../../..
RUNTIME_REQUIREMENTS := requirements.txt
LAMBDA_PLATFORM := manylinux2014_aarch64

define build_phase4_function
	mkdir -p "$(ARTIFACTS_DIR)/mr_lister" "$(ARTIFACTS_DIR)/config/product_profiles"
	cp -R "$(REPOSITORY_ROOT)/src/mr_lister/." "$(ARTIFACTS_DIR)/mr_lister/"
	cp phase4_lambda.py "$(ARTIFACTS_DIR)/phase4_lambda.py"
	cp "$(REPOSITORY_ROOT)"/config/product_profiles/*.json "$(ARTIFACTS_DIR)/config/product_profiles/"
	python3 -m pip install \
		--implementation cp \
		--only-binary=:all: \
		--platform "$(LAMBDA_PLATFORM)" \
		--python-version 3.13 \
		--requirement "$(RUNTIME_REQUIREMENTS)" \
		--target "$(ARTIFACTS_DIR)"
endef

build-PrepareFunction:
	$(build_phase4_function)

build-RegisterApprovalWaitFunction:
	$(build_phase4_function)

build-ApprovalFunction:
	$(build_phase4_function)

build-FakePublishFunction:
	$(build_phase4_function)

build-FakeVerifyFunction:
	$(build_phase4_function)
